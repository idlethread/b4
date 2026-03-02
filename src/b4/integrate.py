# SPDX-License-Identifier: GPL-2.0-or-later
#
# b4 integrate: batch-integrate patch series using shazam
#

import os
from ruamel.yaml import YAML
import traceback
import b4
import b4.command as command


class SkipBranch(Exception):
    pass


def resolve_git_ref(ref):
    try:
        resolved = b4.git_get_command_lines(None, ['rev-parse', '--verify', ref])[0].strip()
        return resolved
    except Exception:
        raise RuntimeError(f'Invalid git ref: {ref}')


def run_integrate(args):
    yaml_path = args.yaml_file
    update_config = args.update_config

    cfg = load_config(yaml_path)
    b4.logger.info(f'Loaded config: {cfg}')

    orig_branch, orig_commit = git_snapshot()
    if args.base:
        base = resolve_git_ref(args.base)
    else:
        base = orig_commit

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
    yaml = YAML()
    yaml.preserve_quotes = True

    with open(path, 'r', encoding='utf-8') as f:
        data = yaml.load(f)

    if not isinstance(data, dict):
        raise RuntimeError('Top-level YAML must be a mapping')

    # Validate structure (but do not destroy comments)
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
        while True:
            try:
                b4.logger.info(f'Applying {msgid}')
                run_shazam_for_msgid(msgid)
                resolved.append(msgid)
                break  # success → next msgid

            except RuntimeError:
                action = handle_shazam_failure(msgid)

                if action == "continue":
                    resolved.append(msgid)
                    break  # move to next msgid WITHOUT retrying

                elif action == "retry":
                    continue  # retry same msgid

                elif action == "skip":
                    break  # move to next msgid WITHOUT appending

                elif action == "abort":
                    raise

    return resolved

def run_shazam_for_msgid(msgid):
    parser = command.setup_parser()
    shazam_args = parser.parse_args(['shazam', '-l', msgid])

    try:
        b4.logger.info(f'Applying {msgid}')
        command.cmd_shazam(shazam_args)
    except SystemExit as e:
        if e.code not in (0, None):
            raise RuntimeError(f'shazam failed for {msgid}')


def handle_shazam_failure(msgid):
    b4.logger.error(f'Shazam failed for {msgid}')
    b4.logger.error('Repository left in current state.')
    b4.logger.error('You may resolve conflicts manually.')

    while True:
        print()
        print("Options:")
        print("  [c] Continue after fixing manually")
        print("  [r] Retry this message-id")
        print("  [s] Skip this message-id")
        print("  [a] Abort branch")
        choice = input("> ").strip().lower()

        if choice == 'c':
            if git_am_in_progress():
                print("git am still in progress. Finish it first.")
            else:
                return "continue"

        elif choice == 'r':
            return "retry"

        elif choice == 's':
            b4.logger.warning(f"Skipping {msgid}")
            return "skip"

        elif choice == 'a':
            return "abort"


def git_am_in_progress():
    gitdir = b4.git_get_command_lines(None, ['rev-parse', '--git-dir'])[0].strip()
    return os.path.exists(os.path.join(gitdir, 'rebase-apply'))


def write_updated_config(path, yaml_data, new_cfg):
    yaml = YAML()
    yaml.preserve_quotes = True
    yaml.indent(mapping=2, sequence=4, offset=2)

    # Update only the branch message-id lists in-place
    for branch, new_ids in new_cfg.items():
        if branch in yaml_data:
            # Clear existing list but keep YAML node structure
            yaml_data[branch].clear()

            # Re-add updated message-ids
            for msgid in new_ids:
                yaml_data[branch].append(msgid)

    tmp = f'{path}.new'
    with open(tmp, 'w', encoding='utf-8') as f:
        yaml.dump(yaml_data, f)

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
