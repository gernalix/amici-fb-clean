import os
import subprocess
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import contextlib
import io
import tempfile
import unittest

import amici_fb as app
import amici_fb_task_runner as runner

ROOT = Path(__file__).resolve().parents[1]


def _check_symlink_outside_repo_and_argument_forwarding(tmp_path):
    link = tmp_path / 'bin' / 'amici_fb'
    link.parent.mkdir()
    link.symlink_to(ROOT / 'scripts/run_fedora.sh')
    # A fake interpreter records cwd/argv without touching Facebook or runtime data.
    python = tmp_path / 'python'
    python.write_text('#!/bin/bash\nprintf "%s\\n" "$PWD" "$@"\n')
    python.chmod(0o755)
    env = dict(os.environ, PYTHON_BIN=str(python),
               AMICI_FB_STORAGE_STATE=str(tmp_path / 'state.json'),
               AMICI_FB_CHROMIUM_PROFILE=str(tmp_path / 'profile'),
               AMICI_FB_OUTPUT_DIR=str(tmp_path / 'output'))
    result = subprocess.run([str(link), '--db', 'custom.sqlite', '--headless'],
                            cwd='/tmp', env=env, text=True, capture_output=True)
    assert result.returncode == 0
    assert result.stdout.splitlines() == [str(ROOT), '-u', '-X', 'faulthandler',
                                          str(ROOT / 'amici_fb_task_runner.py'),
                                          '--db', 'custom.sqlite', '--headless']
    result = subprocess.run([str(link)], cwd='/tmp', env=env,
                            text=True, capture_output=True)
    assert result.returncode == 0
    assert result.stdout.splitlines()[-1] == '--headless'


def _check_snapshot_pipeline_reports_current_diff_only(tmp_path, monkeypatch, old, new, expected):
    if old is not None:
        old = {app.friend_dict_key(url, app.extract_profile_id(url)): (app.canonical_profile_url(url), name) for url, name in old.values()}
    new = {app.friend_dict_key(url, app.extract_profile_id(url)): (app.canonical_profile_url(url), name) for url, name in new.values()}
    monkeypatch.setenv('AMICI_FB_OUTPUT_DIR', str(tmp_path))
    monkeypatch.setattr(sys, 'argv', ['amici_fb', '--headless', '--db', str(tmp_path / 'test.sqlite')])
    if old is not None:
        app.save_snapshot_csv(str(tmp_path / 'amici_2000-01-01T000000Z.csv'), '2000-01-01T00:00:00Z', old)
    context = MagicMock()
    context.browser = None
    page = MagicMock()
    replacements = {
        'sync_playwright': MagicMock(), 'chromium_profile_initialized': lambda _: True,
        'ensure_chromium_profile_dir': lambda: tmp_path / 'profile',
        'attach_context_debug': MagicMock(), 'log_browser_state': MagicMock(),
        'get_own_numeric_id': MagicMock(), 'new_debug_page': lambda *a: page,
        'goto_with_debug': MagicMock(), 'ensure_browser_page_alive': MagicMock(),
        'detect_facebook_special_state': lambda _: (None, None),
        'ensure_known_facebook_state': MagicMock(), 'scroll_friends_page': MagicMock(),
        'extract_friends': lambda *a, **kw: new, 'CHECK_REMOVED_VISIBILITY': False,
    }
    replacements['sync_playwright'].return_value.__enter__.return_value.chromium.launch_persistent_context.return_value = context
    for name, value in replacements.items():
        monkeypatch.setattr(app, name, value)
    monkeypatch.setattr(app.telegram_notify, 'validate_config', MagicMock())
    monkeypatch.setattr(app.telegram_notify, 'config_summary', lambda: 'fixture')
    monkeypatch.setattr(app.telegram_notify, 'notify', MagicMock())
    monkeypatch.setattr(app.telegram_notify, 'send_alert', MagicMock(return_value='sent'))
    monkeypatch.setattr(app.telegram_notify, 'send_file', MagicMock())
    report = app.main()
    for fragment in expected:
        assert fragment in report
    assert 'CSV: ' in report
    assert '2000-01-01' not in report
    assert len(list(tmp_path.glob('amici_*.csv'))) == (2 if old is not None else 1)
    if old is not None:
        assert len(list(tmp_path.glob('diff_*.csv'))) == 1
    # Notification failure must not publish a success report.
    if old != new:
        monkeypatch.setattr(app.telegram_notify, 'send_alert', MagicMock(side_effect=RuntimeError('notify failed')))
        monkeypatch.setattr(app, 'find_previous_csv', lambda _: None)
        with unittest.TestCase().assertRaisesRegex(RuntimeError, 'notify failed'):
            app.main()


def _check_runner_success_and_error_exit_codes(tmp_path, monkeypatch, capsys, failure):
    monkeypatch.setenv('AMICI_FB_OUTPUT_DIR', str(tmp_path))
    monkeypatch.setattr(sys, 'argv', ['amici_fb', '--headless'])
    monkeypatch.setattr(runner, '_load_project_env', lambda _: None)
    monkeypatch.setattr(runner, 'push_kuma', MagicMock())
    def run(*args, **kwargs):
        print('diagnostic detail')
        if failure:
            raise failure
        return {'CLI_RESULT': 'Amici: 1 → 1\nNessuna variazione.\nCSV: fixture.csv'}
    monkeypatch.setattr(runner.runpy, 'run_path', run)
    assert runner.main() == (7 if isinstance(failure, SystemExit) else 1 if failure else 0)
    output = capsys.readouterr()
    if failure:
        assert output.out == ''
        assert 'Errore:' in output.err
    else:
        assert 'Nessuna variazione.' in output.out
        assert 'diagnostic' not in output.out
        assert output.err == ''
    logs = list((tmp_path / 'logs').glob('*.log'))
    assert len(logs) == 1
    assert 'diagnostic detail' in logs[0].read_text()
    assert logs[0].stat().st_mode & 0o777 == 0o600


def _check_systemd_uses_canonical_cli():
    assert 'ExecStart=%h/.local/bin/amici_fb\n' in (ROOT / 'systemd/amici-fb.service').read_text()


class Patches:
    def __init__(self, stack):
        self.stack = stack

    def setenv(self, key, value):
        self.stack.enter_context(patch.dict(os.environ, {key: value}))

    def setattr(self, obj, key, value):
        self.stack.enter_context(patch.object(obj, key, value))


class Capture:
    def __init__(self, stack):
        self.out = io.StringIO()
        self.err = io.StringIO()
        stack.enter_context(contextlib.redirect_stdout(self.out))
        stack.enter_context(contextlib.redirect_stderr(self.err))

    def readouterr(self):
        from types import SimpleNamespace
        return SimpleNamespace(out=self.out.getvalue(), err=self.err.getvalue())


class CliTests(unittest.TestCase):
    def test_symlink(self):
        with tempfile.TemporaryDirectory() as directory:
            _check_symlink_outside_repo_and_argument_forwarding(Path(directory))

    def test_current_diff_pipeline(self):
        anna = {'1': ('https://www.facebook.com/1', 'Anna')}
        bruno = {'2': ('https://www.facebook.com/2', 'Bruno')}
        cases = [
            (anna, bruno, ['1 → 1', 'variazione netta: +0', 'Rimossi: 1',
                           'Anna — https://www.facebook.com/1', 'Aggiunti: 1',
                           'Bruno — https://www.facebook.com/2']),
            ({}, bruno, ['0 → 1', 'variazione netta: +1', 'Aggiunti: 1']),
            (anna, {}, ['1 → 0', 'variazione netta: -1', 'Rimossi: 1']),
            (anna, anna, ['1 → 1', 'Nessuna variazione.']),
            (None, {}, ['non disponibile → 0', 'Primo snapshot']),
        ]
        for old, new, expected in cases:
            with self.subTest(expected=expected), tempfile.TemporaryDirectory() as directory, contextlib.ExitStack() as stack:
                Capture(stack)
                _check_snapshot_pipeline_reports_current_diff_only(Path(directory), Patches(stack), old, new, expected)

    def test_runner_exit_codes(self):
        for failure in (None, RuntimeError('extract failed'), SystemExit(7)):
            with self.subTest(failure=failure), tempfile.TemporaryDirectory() as directory, contextlib.ExitStack() as stack:
                _check_runner_success_and_error_exit_codes(Path(directory), Patches(stack), Capture(stack), failure)

    def test_unit_entrypoint(self):
        _check_systemd_uses_canonical_cli()
