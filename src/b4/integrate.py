# SPDX-License-Identifier: GPL-2.0-or-later
#
# b4 integrate: batch-integrate patch series using shazam
#

import os
import yaml
import traceback
import b4
import b4.command as command


class SkipBranch(Exception):
    pass


def run_integrate(args):
    yaml_path = args.yaml_file
    base = args.base
    update_config = args.update_config

    cfg = load_config(yaml_path)
    b4.logger.info(f'Loaded config: {cfg}')

    orig_branch, orig_commit = git_snapshot()

    results = {
        'success': [],
        'skipped': [],
        'failed': {},
    }

    updated_cfg = {}

    for branch, msgids in cfg.items():
        b4.logger.info(f'Processing branch {branch}')
        try:
            new_ids = integrate_branch(branch, msgids, base, args)
            results['success'].append(branch)
            updated_cfg[branch] = new_ids
        except SkipBranch as e:
            b4.logger.warning(f'Skipping {branch}: {e}')
            results['skipped'].append(branch)
        except Exception as e:
            b4.logger.error(f'Failed {branch}: {e}')
            b4.logger.debug(traceback.format_exc())
            results['failed'][branch] = str(e)
        finally:
            git_restore(orig_branch, orig_commit)

    print_summary(results)

    if update_config:
        write_updated_config(yaml_path, cfg, updated_cfg)


def load_config(path):
    with open(path, 'r', encoding='utf-8') as f:
        data = yaml.safe_load(f) or {}

    if not isinstance(data, dict):
        raise RuntimeError('Top-level YAML must be a mapping')

    for k, v in data.items():
        if not isinstance(v, list):
            raise RuntimeError(f'Branch {k} must map to a list of message-ids')

    return data


def git_snapshot():
    branch = b4.git_get_command_lines(
        None,
        ['rev-parse', '--abbrev-ref', 'HEAD']
    )[0].strip()

    commit = b4.git_get_command_lines(
        None,
        ['rev-parse', 'HEAD']
    )[0].strip()

    return branch, commit


def git_restore(branch, commit):
    b4.git_run_command(None, ['checkout', branch])
    b4.git_run_command(None, ['reset', '--hard', commit])
    b4.git_run_command(None, ['clean', '-fdx'])


def integrate_branch(branch, msgids, base, parent_args):
    if not msgids:
        raise SkipBranch('No message-ids specified')

    b4.git_run_command(None, ['checkout', base])
    b4.git_run_command(None, ['checkout', '-B', branch])

    resolved = []

    for msgid in msgids:
        run_shazam_for_msgid(msgid)
	# Needed when latest msgid is different from original
        resolved.append(msgid)

    return resolved


def run_shazam_for_msgid(msgid):
    # Reuse the real parser to build a valid namespace
    parser = command.setup_parser()

    shazam_args = parser.parse_args(['shazam', '-l', msgid])

    try:
        b4.logger.info(f'Applying {msgid}')
        # This ensures subcmd, config, and everything else is populated
        command.cmd_shazam(shazam_args)
    except SystemExit as e:
        if e.code not in (0, None):
            raise RuntimeError(f'shazam failed for {msgid}')


def write_updated_config(path, old_cfg, new_cfg):
    merged = {}
    for branch, old_ids in old_cfg.items():
        merged[branch] = new_cfg.get(branch, old_ids)

    tmp = f'{path}.new'
    with open(tmp, 'w', encoding='utf-8') as f:
        yaml.safe_dump(merged, f, sort_keys=False)

    os.replace(tmp, path)
    b4.logger.info(f'Updated {path}')


def print_summary(results):
    b4.logger.info('Integration summary')

    b4.logger.info(f'Successful branches: {len(results["success"])}')
    for b in results['success']:
        b4.logger.info(f'  ✓ {b}')

    b4.logger.info(f'Skipped branches: {len(results["skipped"])}')
    for b in results['skipped']:
        b4.logger.info(f'  - {b}')

    b4.logger.info(f'Failed branches: {len(results["failed"])}')
    for b, err in results['failed'].items():
        b4.logger.error(f'  ✗ {b}: {err}')
