import tempfile
import yaml

import b4.integrate as integrate


class DummySeries:
    def __init__(self, msgid):
        self.msgid = msgid

    def get_latest_revision(self):
        return self


def test_load_config(tmp_path):
    cfg = {
        'branch-a': ['msgid1', 'msgid2'],
        'branch-b': ['msgid3'],
    }
    p = tmp_path / 'cfg.yaml'
    p.write_text(yaml.safe_dump(cfg))

    loaded = integrate.load_config(p)
    assert loaded == cfg


def test_integrate_branch_success(monkeypatch):
    calls = []

    monkeypatch.setattr(
        integrate.b4,
        'git_run_command',
        lambda cmd: calls.append(cmd)
    )

    monkeypatch.setattr(
        integrate.lore,
        'get_series_by_msgid',
        lambda mid: DummySeries(mid)
    )

    monkeypatch.setattr(
        integrate.shazam,
        'run_shazam',
        lambda args: 0
    )

    res = integrate.integrate_branch(
        'test-branch',
        ['<id1>', '<id2>'],
        'HEAD'
    )

    assert res == ['<id1>', '<id2>']
    assert any('-B' in cmd for cmd in calls)


def test_integrate_branch_missing_series(monkeypatch):
    monkeypatch.setattr(
        integrate.lore,
        'get_series_by_msgid',
        lambda mid: None
    )

    try:
        integrate.integrate_branch('b', ['<id>'], 'HEAD')
        assert False, 'Expected failure'
    except RuntimeError as e:
        assert 'Unable to locate series' in str(e)


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
