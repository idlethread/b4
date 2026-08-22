import argparse
import yaml

import b4.integrate as integrate


def test_load_config(tmp_path):
    cfg = {
        'branch-a': ['msgid1', 'msgid2'],
        'branch-b': ['msgid3'],
    }
    p = tmp_path / 'cfg.yaml'
    p.write_text(yaml.safe_dump(cfg))

    loaded = integrate.load_config(p)
    assert loaded == cfg


# ---------------------------------------------------------------------------
# strip_lore_url: lore.kernel.org URLs are accepted as message-id aliases
# ---------------------------------------------------------------------------

def test_strip_lore_url_bare_msgid_unchanged():
    mid = '20260219-msm-device-id-v1-1-9e7315a6fd20@oss.qualcomm.com'
    assert integrate.strip_lore_url(mid) == mid


def test_strip_lore_url_strips_angle_brackets():
    assert integrate.strip_lore_url('<abc@example.org>') == 'abc@example.org'


def test_strip_lore_url_list_variants():
    mid = '20260215-anx-fix-v1-1-75172a5ca88b@oss.qualcomm.com'
    for url in (
        f'https://lore.kernel.org/all/{mid}/',
        f'https://lore.kernel.org/r/{mid}',
        f'https://lore.kernel.org/linux-arm-msm/{mid}/',
        f'https://lore.kernel.org/linux-arm-msm/{mid}/T/#u',
        f'http://lore.kernel.org/{mid}',
    ):
        assert integrate.strip_lore_url(url) == mid, url


def test_strip_lore_url_non_lore_url_passes_through():
    # A patchwork-style URL is not a lore URL; leave it for b4 to handle later.
    other = 'https://patchwork.kernel.org/project/x/patch/foo@bar/'
    assert integrate.strip_lore_url(other) == other


def test_load_config_normalizes_lore_urls(tmp_path):
    mid = '20260213101002.105238-1-r.mereu.kernel@arduino.cc'
    cfg = {'unoq': [f'https://lore.kernel.org/all/{mid}/', f'<{mid}>']}
    p = tmp_path / 'cfg.yaml'
    p.write_text(yaml.safe_dump(cfg))

    loaded = integrate.load_config(p)
    assert loaded == {'unoq': [mid, mid]}


def test_integrate_branch_success(monkeypatch):
    calls = []

    monkeypatch.setattr(
        integrate.b4,
        'git_run_command',
        lambda gitdir, cmd: calls.append(cmd)
    )

    monkeypatch.setattr(
        integrate,
        'run_shazam_for_msgid',
        lambda msgid: None
    )

    dummy_args = argparse.Namespace(update_config=False)
    res, had_conflict = integrate.integrate_branch(
        'test-branch',
        ['<id1>', '<id2>'],
        'HEAD',
        dummy_args,
    )

    assert res == ['<id1>', '<id2>']
    assert had_conflict is False
    assert any('-B' in cmd for cmd in calls)


def test_integrate_branch_abort(monkeypatch):
    monkeypatch.setattr(
        integrate.b4,
        'git_run_command',
        lambda gitdir, cmd: None
    )

    monkeypatch.setattr(
        integrate,
        'run_shazam_for_msgid',
        lambda msgid: (_ for _ in ()).throw(RuntimeError('shazam failed'))
    )

    monkeypatch.setattr(
        integrate,
        'handle_shazam_failure',
        lambda msgid: 'abort'
    )

    dummy_args = argparse.Namespace(update_config=False)
    try:
        integrate.integrate_branch('b', ['<id>'], 'HEAD', dummy_args)
        assert False, 'Expected RuntimeError'
    except RuntimeError:
        pass


def test_write_updated_config(tmp_path):
    old = {
        'a': ['old1'],
        'b': ['old2'],
    }
    new = {
        'a': ['new1'],
    }

    p = tmp_path / 'cfg.yaml'
    p.write_text(yaml.safe_dump(old))

    integrate.write_updated_config(p, old, new)

    updated = yaml.safe_load(p.read_text())
    assert updated['a'] == ['new1']
    assert updated['b'] == ['old2']


def test_write_updated_config_preserves_comments(tmp_path):
    """Rewriting must keep the '# title — author' comments and formatting."""
    content = (
        '# generated file, do not hand-edit lightly\n'
        'glymur:\n'
        '  # arm64: dts: qcom: add glymur — Jane Dev <jane@example.org>\n'
        '  - 20260101-glymur-v1-0-aaaa@example.org\n'
        '  # power: add rpmh regulators — John Rev <john@example.org>\n'
        '  - 20260102-rpmh-v2-0-bbbb@example.org\n'
        '\n'
        'hamoa:\n'
        '  - 20260103-hamoa-v1-0-cccc@example.org\n'
    )
    p = tmp_path / 'series.yaml'
    p.write_text(content)

    old = {
        'glymur': ['20260101-glymur-v1-0-aaaa@example.org',
                   '20260102-rpmh-v2-0-bbbb@example.org'],
        'hamoa': ['20260103-hamoa-v1-0-cccc@example.org'],
    }
    new = {
        # only the second glymur entry was rerolled
        'glymur': ['20260101-glymur-v1-0-aaaa@example.org',
                   '20260115-rpmh-v3-0-dddd@example.org'],
        'hamoa': ['20260103-hamoa-v1-0-cccc@example.org'],
    }

    integrate.write_updated_config(p, old, new)
    result = p.read_text()

    # Comments survive verbatim.
    assert '# arm64: dts: qcom: add glymur — Jane Dev <jane@example.org>' in result
    assert '# power: add rpmh regulators — John Rev <john@example.org>' in result
    assert '# generated file, do not hand-edit lightly' in result
    # The blank line between the two branches survives.
    assert '\n\nhamoa:' in result
    # The changed id was swapped, the unchanged ones left alone.
    assert '20260115-rpmh-v3-0-dddd@example.org' in result
    assert '20260102-rpmh-v2-0-bbbb@example.org' not in result
    assert '20260101-glymur-v1-0-aaaa@example.org' in result
    # And the file still parses to the expected structure.
    assert yaml.safe_load(result) == new


def test_write_updated_config_preserves_quotes(tmp_path):
    """A quoted value keeps its quotes when only the id inside changes."""
    content = 'b:\n  - "old@example.org"\n'
    p = tmp_path / 'series.yaml'
    p.write_text(content)

    integrate.write_updated_config(
        p, {'b': ['old@example.org']}, {'b': ['new@example.org']})
    result = p.read_text()
    assert result == 'b:\n  - "new@example.org"\n'


def test_write_updated_config_only_untouched_branch_not_rewritten(tmp_path):
    """A branch absent from new_cfg (e.g. skipped by --only) is left verbatim."""
    content = (
        'a:\n'
        '  # keep me — Someone <s@example.org>\n'
        '  - keep@example.org\n'
        'b:\n'
        '  - old@example.org\n'
    )
    p = tmp_path / 'series.yaml'
    p.write_text(content)

    # new_cfg only carries 'b'; 'a' must be preserved comment and all.
    integrate.write_updated_config(
        p,
        {'a': ['keep@example.org'], 'b': ['old@example.org']},
        {'b': ['new@example.org']},
    )
    result = p.read_text()
    assert '# keep me — Someone <s@example.org>' in result
    assert 'keep@example.org' in result
    assert 'new@example.org' in result
    assert 'old@example.org' not in result


# ---------------------------------------------------------------------------
# --only BRANCH: repeatable branch filter (select_branches)
# ---------------------------------------------------------------------------

def test_select_branches_no_filter_returns_all():
    cfg = {'a': ['<1>'], 'b': ['<2>']}
    assert integrate.select_branches(cfg, None) is cfg
    assert integrate.select_branches(cfg, []) is cfg


def test_select_branches_keeps_only_named_in_config_order():
    cfg = {'a': ['<1>'], 'b': ['<2>'], 'c': ['<3>']}
    # Order of the filter must not matter; config order is preserved.
    got = integrate.select_branches(cfg, ['c', 'a'])
    assert list(got.keys()) == ['a', 'c']
    assert got == {'a': ['<1>'], 'c': ['<3>']}


def test_select_branches_dedupes_and_warns_on_unknown(caplog):
    cfg = {'a': ['<1>']}
    got = integrate.select_branches(cfg, ['a', 'a', 'missing'])
    assert got == {'a': ['<1>']}
    assert 'missing' in caplog.text


def test_run_integrate_only_preserves_untouched_branches_on_update(monkeypatch, tmp_path):
    """--only processes a subset but --update-config must keep the rest."""
    cfg = {
        'keep-me': ['<untouched>'],
        'process-me': ['<applied>'],
    }
    p = tmp_path / 'series.yaml'
    p.write_text(yaml.safe_dump(cfg, sort_keys=False))

    _quiet_git(monkeypatch)
    monkeypatch.setattr(integrate, 'git_snapshot', lambda: ('main', 'abc1234'))
    monkeypatch.setattr(integrate, 'git_restore', lambda branch, commit: None)
    monkeypatch.setattr(integrate, 'run_shazam_for_msgid', lambda msgid: None)
    monkeypatch.setattr(integrate, 'resolve_latest_msgid', lambda msgid: msgid)
    monkeypatch.setattr(integrate, 'print_summary', lambda results: None)

    processed = []
    real_integrate_branch = integrate.integrate_branch

    def spy(branch, msgids, base, cmdargs):
        processed.append(branch)
        return real_integrate_branch(branch, msgids, base, cmdargs)

    monkeypatch.setattr(integrate, 'integrate_branch', spy)

    args = argparse.Namespace(
        yaml_file=str(p),
        base='HEAD',
        update_config=True,
        only=['process-me'],
    )
    integrate.run_integrate(args)

    # Only the requested branch was built ...
    assert processed == ['process-me']
    # ... but the rewritten file still carries the untouched branch verbatim.
    written = yaml.safe_load(p.read_text())
    assert written['keep-me'] == ['<untouched>']
    assert 'process-me' in written


# ---------------------------------------------------------------------------
# Topic-integration feature cases: merged, skipped, and conflict.
#
# These exercise the per-branch apply loop (integrate_branch) and the
# end-to-end orchestration (run_integrate), covering the three outcomes a
# branch can have: cleanly merged, skipped, and a shazam conflict resolved
# through the interactive failure menu.
# ---------------------------------------------------------------------------


def _quiet_git(monkeypatch):
    """Stub git so integrate_branch/run_integrate touch no real repo."""
    monkeypatch.setattr(
        integrate.b4,
        'git_run_command',
        lambda gitdir, cmd: (0, ''),
    )


# --- skipped case -----------------------------------------------------------

def test_integrate_branch_skip_empty(monkeypatch):
    """A branch with no message-ids is skipped via SkipBranch."""
    _quiet_git(monkeypatch)

    dummy_args = argparse.Namespace(update_config=False)
    try:
        integrate.integrate_branch('empty-branch', [], 'HEAD', dummy_args)
        assert False, 'Expected SkipBranch'
    except integrate.SkipBranch:
        pass


# --- conflict cases (interactive failure menu) ------------------------------

def test_integrate_branch_conflict_continue(monkeypatch):
    """shazam fails; user resolves manually and continues -> msgid kept."""
    _quiet_git(monkeypatch)
    monkeypatch.setattr(
        integrate,
        'run_shazam_for_msgid',
        lambda msgid: (_ for _ in ()).throw(RuntimeError('conflict')),
    )
    monkeypatch.setattr(integrate, 'handle_shazam_failure', lambda msgid: 'continue')

    dummy_args = argparse.Namespace(update_config=False)
    res, had_conflict = integrate.integrate_branch('c', ['<id>'], 'HEAD', dummy_args)
    # 'continue' means the conflict was resolved by hand, so the id is kept.
    assert res == ['<id>']
    assert had_conflict is True


def test_integrate_branch_conflict_skip(monkeypatch):
    """shazam fails; user skips the msgid -> not included in results."""
    _quiet_git(monkeypatch)
    monkeypatch.setattr(
        integrate,
        'run_shazam_for_msgid',
        lambda msgid: (_ for _ in ()).throw(RuntimeError('conflict')),
    )
    monkeypatch.setattr(integrate, 'handle_shazam_failure', lambda msgid: 'skip')

    dummy_args = argparse.Namespace(update_config=False)
    res, had_conflict = integrate.integrate_branch('c', ['<id1>', '<id2>'], 'HEAD', dummy_args)
    # Both conflict and are skipped, so nothing is appended.
    assert res == []
    assert had_conflict is False


def test_integrate_branch_conflict_retry_then_success(monkeypatch):
    """shazam fails once, user retries, second attempt succeeds."""
    _quiet_git(monkeypatch)

    attempts = {'<id>': 0}

    def flaky_shazam(msgid):
        attempts[msgid] += 1
        if attempts[msgid] == 1:
            raise RuntimeError('conflict')
        # second attempt succeeds

    monkeypatch.setattr(integrate, 'run_shazam_for_msgid', flaky_shazam)
    monkeypatch.setattr(integrate, 'handle_shazam_failure', lambda msgid: 'retry')

    dummy_args = argparse.Namespace(update_config=False)
    res, had_conflict = integrate.integrate_branch('c', ['<id>'], 'HEAD', dummy_args)
    assert res == ['<id>']
    assert had_conflict is False
    assert attempts['<id>'] == 2


# --- end-to-end orchestration: all three outcomes in one run ---------------

def test_run_integrate_merged_skipped_conflict(monkeypatch, tmp_path):
    """run_integrate classifies branches into success / skipped / failed."""
    cfg = {
        'merged': ['ok1', 'ok2'],  # applies cleanly -> success
        'skipped': [],             # no msgids -> skipped
        'conflict': ['bad'],       # shazam fails, user aborts -> failed
    }
    p = tmp_path / 'series.yaml'
    p.write_text(yaml.safe_dump(cfg, sort_keys=False))

    _quiet_git(monkeypatch)
    monkeypatch.setattr(integrate, 'git_snapshot', lambda: ('main', 'abc1234'))
    restore_calls = []
    monkeypatch.setattr(
        integrate,
        'git_restore',
        lambda branch, commit: restore_calls.append((branch, commit)),
    )

    def fake_shazam(msgid):
        if msgid == 'bad':
            raise RuntimeError('conflict')

    monkeypatch.setattr(integrate, 'run_shazam_for_msgid', fake_shazam)

    handled = []

    def fake_failure(msgid):
        handled.append(msgid)
        return 'abort'

    monkeypatch.setattr(integrate, 'handle_shazam_failure', fake_failure)

    captured = {}
    monkeypatch.setattr(integrate, 'print_summary', lambda results: captured.update(results))

    args = argparse.Namespace(
        yaml_file=str(p),
        base='HEAD',
        update_config=False,
    )
    integrate.run_integrate(args)

    assert captured['success'] == ['merged']
    assert 'skipped' in captured['skipped']          # key in the skipped dict
    assert 'conflict' in captured['failed']
    # The conflict branch went through the interactive failure handler.
    assert handled == ['bad']
    # git state is restored once per branch (in the finally clause).
    assert len(restore_calls) == len(cfg)


def test_print_summary_reports_all_three(capsys):
    """print_summary surfaces merged, skipped, and conflict outcomes."""
    results = {
        'success': ['clean-branch'],
        'conflicts_resolved': ['conflict-branch'],
        'skipped': {'skipped-branch': 'No message-ids specified'},
        'failed': {'failed-branch': 'shazam failed for <bad>'},
    }
    integrate.print_summary(results)
    out = capsys.readouterr().out

    assert 'clean-branch' in out
    assert 'merged' in out
    assert 'conflict-branch' in out
    assert 'conflict' in out                           # status column
    assert 'skipped-branch' in out
    assert 'skipped' in out                            # status column
    assert 'No message-ids specified' in out           # reason in Notes
    assert 'failed-branch' in out
    assert 'FAILED' in out
    assert 'shazam failed for <bad>' in out
