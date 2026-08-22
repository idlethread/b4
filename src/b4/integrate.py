#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# SPDX-License-Identifier: GPL-2.0-or-later
# Copyright (C) 2026 by the Linux Foundation
#
# b4 integrate: batch-build integration branches from tracked patch series
#
__author__ = 'Amit Kucheria <amit.kucheria@oss.qualcomm.com>'

import argparse
import os
import re
import sys
import traceback
import urllib.parse

from pathlib import Path
from typing import Dict, List, Optional, Tuple, TypedDict, Union

import yaml

import b4
import b4.command as command
import b4.mbox

logger = b4.logger

# Cache the full CLI parser so we do not rebuild every subparser for each
# message-id handed off to shazam (see _build_shazam_args()).
_shazam_parser: Optional[argparse.ArgumentParser] = None


class IntegrationResults(TypedDict):
    success: List[str]               # branches applied without any manual intervention
    conflicts_resolved: List[str]    # branches that needed a manual conflict resolution
    already_upstream: Dict[str, str] # branch -> note; series produced no diff vs base
    skipped: Dict[str, str]          # branch -> reason (e.g. "No message-ids specified")
    failed: Dict[str, str]           # branch -> failure reason


class SkipBranch(Exception):
    """Raised to bucket a branch as skipped rather than failed."""


class AlreadyUpstream(Exception):
    """Raised when a built branch's tree matches its base, i.e. the series
    changed nothing and is already present upstream."""


def run_integrate(cmdargs: argparse.Namespace) -> None:
    yaml_path = cmdargs.yaml_file
    base = cmdargs.base
    update_config = cmdargs.update_config

    full_cfg = load_config(yaml_path)
    logger.debug('Loaded config: %s', full_cfg)

    # --update-config rewrites the file in place, so it must see every branch --
    # even the ones a --only run does not touch -- or they would be dropped.
    # We therefore process a filtered view but always hand the full config to
    # write_updated_config().
    cfg = select_branches(full_cfg, getattr(cmdargs, 'only', None))

    orig_branch, orig_commit = git_snapshot()

    results: IntegrationResults = {
        'success': [],
        'conflicts_resolved': [],
        'skipped': {},
        'failed': {},
    }

    updated_cfg: Dict[str, List[str]] = {}

    for branch, msgids in cfg.items():
        logger.info('Processing branch %s', branch)
        try:
            new_ids, had_conflict = integrate_branch(branch, msgids, base, cmdargs)
            if had_conflict:
                results['conflicts_resolved'].append(branch)
            else:
                results['success'].append(branch)
            updated_cfg[branch] = new_ids
        except SkipBranch as ex:
            logger.warning('Skipping %s: %s', branch, ex)
            results['skipped'][branch] = str(ex)
        except Exception as ex:  # one bad branch must not abort the whole run
            logger.error('Failed %s: %s', branch, ex)
            logger.debug(traceback.format_exc())
            results['failed'][branch] = str(ex)
        finally:
            git_restore(orig_branch, orig_commit)

    print_summary(results)

    if update_config:
        write_updated_config(yaml_path, full_cfg, updated_cfg)


def select_branches(cfg: Dict[str, List[str]],
                    only: Optional[List[str]]) -> Dict[str, List[str]]:
    """Return the branches to process, honouring a repeatable --only filter.

    Without --only the whole config is returned. With --only, only the named
    branches are kept, in the config's own order; names that do not exist in
    the config are warned about and ignored.
    """
    if not only:
        return cfg

    wanted = list(dict.fromkeys(only))  # dedupe, keep first-seen order
    for name in wanted:
        if name not in cfg:
            logger.warning('--only %s: no such branch in the config; ignoring', name)

    keep = set(wanted)
    selected = {branch: msgids for branch, msgids in cfg.items() if branch in keep}
    if not selected:
        logger.warning('--only matched no branches; nothing to process')
    return selected


def load_config(path: Union[str, Path]) -> Dict[str, List[str]]:
    with open(path, encoding='utf-8') as f:
        data = yaml.safe_load(f) or {}

    if not isinstance(data, dict):
        raise RuntimeError('Top-level YAML must be a mapping')

    normalized: Dict[str, List[str]] = {}
    for branch, msgids in data.items():
        if not isinstance(msgids, list):
            raise RuntimeError(f'Branch {branch} must map to a list of message-ids')
        # Entries may be bare message-ids or full lore.kernel.org URLs; strip
        # each down to a raw message-id so both forms work interchangeably.
        normalized[branch] = [strip_lore_url(str(m)) for m in msgids]

    return normalized


# Match a lore.kernel.org message URL and capture the message-id. The path is
# https://lore.kernel.org/<list>/<msgid>/<optional thread anchors>, where the
# optional <list> segment may also be 'all' or 'r'. Message-ids always contain
# an '@' and never a '/', which lets us pick them out unambiguously.
_LORE_URL_RE = re.compile(
    r'^https?://lore\.kernel\.org/'
    r'(?:[^/]+/)?'                       # optional list segment (all/, r/, <list>/)
    r'(?P<msgid>[^/]+@[^/]+)'            # the message-id (no slashes, has an '@')
    r'(?:/.*)?$',                        # optional trailing slash + thread anchors
    re.IGNORECASE,
)


def strip_lore_url(entry: str) -> str:
    """Normalise one config entry to a bare message-id.

    Accepts a raw message-id (optionally angle-bracketed) or a full
    lore.kernel.org URL such as ``https://lore.kernel.org/all/<msgid>/`` or
    ``https://lore.kernel.org/<list>/<msgid>/T/#u`` and returns the bare
    ``<msgid>``. Anything that is not a recognised lore URL is returned with
    only surrounding whitespace and angle brackets removed, so unrelated
    strings pass through unchanged.
    """
    s = entry.strip()
    match = _LORE_URL_RE.match(s)
    if match:
        return urllib.parse.unquote(match.group('msgid'))
    return s.strip('<>')


def git_snapshot() -> Tuple[str, str]:
    branch = b4.git_get_command_lines(
        None, ['rev-parse', '--abbrev-ref', 'HEAD'],
    )[0].strip()
    commit = b4.git_get_command_lines(
        None, ['rev-parse', 'HEAD'],
    )[0].strip()

    # In detached-HEAD state, restore by commit SHA rather than the name 'HEAD'.
    return (commit if branch == 'HEAD' else branch), commit


def git_am_in_progress() -> bool:
    gitdir = b4.git_get_command_lines(None, ['rev-parse', '--git-dir'])[0].strip()
    return os.path.exists(os.path.join(gitdir, 'rebase-apply'))


def git_restore(branch: str, commit: str) -> None:
    if git_am_in_progress():
        b4.git_run_command(None, ['am', '--abort'])
    b4.git_run_command(None, ['checkout', branch])
    b4.git_run_command(None, ['reset', '--hard', commit])
    b4.git_run_command(None, ['clean', '-fd'])


def integrate_branch(branch: str, msgids: List[str], base: str,
                     cmdargs: argparse.Namespace) -> Tuple[List[str], bool]:
    if not msgids:
        raise SkipBranch('No message-ids specified')

    b4.git_run_command(None, ['checkout', base])
    b4.git_run_command(None, ['checkout', '-B', branch])

    resolved: List[str] = []
    update_config = getattr(cmdargs, 'update_config', False)
    had_conflict = False

    for msgid in msgids:
        while True:
            try:
                logger.info('Applying %s', msgid)
                run_shazam_for_msgid(msgid)
                if update_config:
                    latest = resolve_latest_msgid(msgid)
                    if latest != msgid:
                        logger.info('Newer revision found for %s: %s', msgid, latest)
                    resolved.append(latest)
                else:
                    resolved.append(msgid)
                break  # success -> next msgid

            except RuntimeError:
                action = handle_shazam_failure(msgid)

                if action == 'continue':
                    # Conflict resolved by hand; record the msgid and advance.
                    had_conflict = True
                    resolved.append(msgid)
                    break
                if action == 'retry':
                    continue  # re-apply the same msgid
                if action == 'skip':
                    break  # advance WITHOUT recording the msgid
                # action == 'abort': bubble up so the branch lands in 'failed'.
                raise

    return resolved, had_conflict


def resolve_latest_msgid(msgid: str) -> str:
    """Return the msgid of the latest revision of the series, or the original."""
    msgs = b4.get_pi_thread_by_msgid(msgid, nocache=True)
    if not msgs:
        return msgid

    msgs = b4.mbox.get_extra_series(msgs, direction=1, nocache=True)

    lmbx = b4.LoreMailbox()
    for msg in msgs:
        lmbx.add_message(msg)

    lser = lmbx.get_series(revision=None, sloppytrailers=False, reroll=True)
    if lser is None:
        return msgid

    # Prefer the cover letter (patches[0]); fall back to the first real patch.
    for patch in lser.patches:
        if patch is not None:
            return str(patch.msgid)

    return msgid


def _build_shazam_args(msgid: str) -> argparse.Namespace:
    # Parse through b4's real CLI parser so the shazam Namespace always carries
    # every option shazam expects -- including ones b4 reads without a getattr
    # fallback, e.g. cmdargs.mergebase -- and stays correct as shazam gains
    # flags. -n forces non-interactive, -C forces no-cache; shazam pins outdir.
    global _shazam_parser
    if _shazam_parser is None:
        _shazam_parser = command.setup_parser()
    return _shazam_parser.parse_args(['-n', 'shazam', '-C', msgid])


def run_shazam_for_msgid(msgid: str) -> None:
    shazam_args = _build_shazam_args(msgid)
    try:
        command.cmd_shazam(shazam_args)
    except SystemExit as ex:
        if ex.code not in (0, None):
            raise RuntimeError(f'shazam failed for {msgid}') from ex


def handle_shazam_failure(msgid: str) -> str:
    logger.error('Shazam failed for %s', msgid)
    logger.error('Repository left in current state.')

    # A non-interactive run cannot answer the prompt; abort the branch rather
    # than blocking on stdin, so it is bucketed as failed and the run continues.
    if not sys.stdin.isatty():
        logger.error('Not running interactively; aborting this branch.')
        return 'abort'

    logger.error('You may resolve conflicts manually.')
    while True:
        print()
        print('Options:')
        print('  [c] Continue after fixing manually')
        print('  [r] Retry this message-id')
        print('  [s] Skip this message-id')
        print('  [a] Abort branch')
        choice = input('> ').strip().lower()

        if choice == 'c':
            if git_am_in_progress():
                print('git am still in progress. Finish it first.')
            else:
                return 'continue'
        elif choice == 'r':
            return 'retry'
        elif choice == 's':
            logger.warning('Skipping %s', msgid)
            return 'skip'
        elif choice == 'a':
            return 'abort'


def write_updated_config(path: Union[str, Path], old_cfg: Dict[str, List[str]],
                        new_cfg: Dict[str, List[str]]) -> None:
    """Rewrite the YAML file in place, updating only the changed message-ids.

    The rewrite is line-wise rather than a full ``yaml.safe_dump`` so that
    hand-authored structure survives untouched -- in particular the
    ``# <series title> — <author>`` comment kept above each entry, plus blank
    lines, quoting style and key order. Only the value on a list-item line
    whose message-id actually changed is edited; every other byte is copied
    verbatim.
    """
    # Positional map of edits: branch -> {list-index: (old_msgid, new_msgid)}.
    changes: Dict[str, Dict[int, Tuple[str, str]]] = {}
    for branch, old_ids in old_cfg.items():
        new_ids = new_cfg.get(branch)
        if new_ids is None:
            continue  # branch was not processed this run (e.g. --only); leave it
        for idx, (old, new) in enumerate(zip(old_ids, new_ids)):
            if old != new:
                changes.setdefault(branch, {})[idx] = (old, new)
                logger.info('  %s: %s -> %s', branch, old, new)

    if not changes:
        logger.info('No message-id updates detected')
        return

    with open(path, encoding='utf-8') as f:
        lines = f.readlines()

    new_lines = _rewrite_config_lines(lines, changes)

    tmp = f'{path}.new'
    with open(tmp, 'w', encoding='utf-8') as f:
        f.writelines(new_lines)

    os.replace(tmp, path)
    logger.info('Updated %s', path)


# A top-level mapping key (branch name) sits at column 0 and ends in ':',
# optionally trailed by a comment. A list item is an indented '- <value>'.
_KEY_RE = re.compile(r'^(?P<key>[^\s#][^:]*):\s*(?:#.*)?$')
_ITEM_RE = re.compile(r'^\s*-\s+\S')


def _rewrite_config_lines(lines: List[str],
                          changes: Dict[str, Dict[int, Tuple[str, str]]]) -> List[str]:
    out: List[str] = []
    branch: Optional[str] = None
    idx = 0
    for line in lines:
        key_match = _KEY_RE.match(line)
        if key_match:
            branch = key_match.group('key')
            idx = 0
            out.append(line)
            continue

        if branch is not None and _ITEM_RE.match(line):
            edit = changes.get(branch, {}).get(idx)
            if edit is not None:
                old, new = edit
                if old in line:
                    line = line.replace(old, new, 1)
                else:
                    logger.warning(
                        'Could not find %s on its line under %s; leaving as-is',
                        old, branch)
            idx += 1
            out.append(line)
            continue

        # Comments, blank lines, and anything else pass through unchanged and
        # do not advance the list-item index.
        out.append(line)

    return out


def print_summary(results: IntegrationResults) -> None:
    """Print a table summarising every branch processed in this run.

    Columns: Branch | Status | Notes
    Status values:
      merged            – applied cleanly, no intervention required
      merged (conflict) – applied after a manual conflict resolution
      skipped           – not processed (reason shown in Notes)
      FAILED            – apply failed and was not recovered

    The table is padded to the widest branch name so all columns line up.
    """
    # Collect all rows so we can compute column widths first.
    rows: List[Tuple[str, str, str]] = []  # (branch, status, notes)
    for branch in results['success']:
        rows.append((branch, 'merged', ''))
    for branch in results['conflicts_resolved']:
        rows.append((branch, 'merged (conflict)', 'manual conflict resolution'))
    for branch, reason in results['skipped'].items():
        rows.append((branch, 'skipped', reason))
    for branch, err in results['failed'].items():
        rows.append((branch, 'FAILED', err))

    if not rows:
        print('\nNo branches processed.')
        return

    COL_BRANCH = 'Branch'
    COL_STATUS = 'Status'
    COL_NOTES  = 'Notes'

    w_branch = max(len(COL_BRANCH), *(len(r[0]) for r in rows))
    w_status = max(len(COL_STATUS), *(len(r[1]) for r in rows))
    # Notes column: cap at 60 chars to keep the table readable in a terminal.
    MAX_NOTES = 60
    w_notes  = min(MAX_NOTES, max(len(COL_NOTES), *(len(r[2]) for r in rows)))

    def sep() -> str:
        return f'+{"-" * (w_branch + 2)}+{"-" * (w_status + 2)}+{"-" * (w_notes + 2)}+'

    def row(branch: str, status: str, notes: str) -> str:
        notes_trunc = notes[:w_notes] if len(notes) > w_notes else notes
        return (
            f'| {branch:<{w_branch}} '
            f'| {status:<{w_status}} '
            f'| {notes_trunc:<{w_notes}} |'
        )

    total = len(rows)
    n_ok  = len(results['success']) + len(results['conflicts_resolved'])
    n_skip = len(results['skipped'])
    n_fail = len(results['failed'])

    print(f'\nIntegration summary  ({total} branch{"es" if total != 1 else ""}: '
          f'{n_ok} merged, {n_skip} skipped, {n_fail} failed)')
    print(sep())
    print(row(COL_BRANCH, COL_STATUS, COL_NOTES))
    print(sep())
    for r in rows:
        print(row(*r))
    print(sep())
