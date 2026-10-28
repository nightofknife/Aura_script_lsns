"""Only startup cleanup may remove registered, inactive log sessions."""
import os
from pathlib import Path
import time

import pytest

from packages.aura_core.observability.logging import coordinator as logs
from packages.aura_core.observability.logging import log_session
from packages.aura_core.observability.logging.core_logger import logger


@pytest.fixture(autouse=True)
def isolated(monkeypatch):
    logs.close_session()
    monkeypatch.delenv("AURA_LOG_DESCRIPTOR", raising=False)
    monkeypatch.delenv("AURA_LOG_SESSION_ID", raising=False)
    yield
    logs.close_session()


def age_session(root, descriptor, seconds):
    stamp = time.time() - seconds
    metadata = root / '.sessions' / descriptor['session_id']
    files = [Path(descriptor['log_path']), *metadata.iterdir()]
    errors = root / 'errors' / descriptor['session_id']
    if errors.exists():
        files += list(errors.iterdir())
    for file in files:
        os.utime(file, (stamp, stamp))


def test_expired_registered_session_and_png_removed_only_on_next_start(tmp_path):
    first = logs.start_session(tmp_path, descriptor={})
    evidence = tmp_path / 'errors' / first['session_id'] / 'test-evidence.png'
    evidence.parent.mkdir(parents=True)
    evidence.write_bytes(b'png-evidence')
    logs.close_session()
    age_session(tmp_path, first, 73*3600)
    assert Path(first['log_path']).exists() and evidence.exists()
    second = logs.start_session(tmp_path, descriptor={})
    assert not Path(first['log_path']).exists()
    assert not evidence.exists()
    assert Path(second['log_path']).exists()


def test_recent_log_and_unknown_file_are_preserved(tmp_path):
    first = logs.start_session(tmp_path, descriptor={})
    logs.close_session()
    age_session(tmp_path, first, 71*3600)
    foreign = tmp_path / 'user-important.log'
    foreign.write_text('keep', encoding='utf-8')
    old = time.time()-100*3600
    os.utime(foreign, (old, old))
    logs.start_session(tmp_path, descriptor={})
    assert Path(first['log_path']).exists() and foreign.read_text() == 'keep'


def test_repeated_setup_and_attach_do_not_repeat_startup_cleanup(tmp_path, monkeypatch):
    calls = []
    original = log_session._cleanup
    def record(*args):
        calls.append(args)
        return original(*args)
    monkeypatch.setattr(log_session, '_cleanup', record)
    first = logs.start_session(tmp_path, descriptor={})
    for _ in range(5):
        logger.setup(log_dir=str(tmp_path), task_name='not-a-new-log')
        logs.attach_session(first)
    assert len(calls) == 1
    assert len(list(tmp_path.glob('*.log*'))) == 1


def test_active_lease_prevents_deletion_even_when_files_are_old(tmp_path):
    descriptor = logs.start_session(tmp_path, descriptor={})
    assert logs.flush()
    age_session(tmp_path, descriptor, 100*3600)
    # _cleanup uses a second OS handle; the active process lease stays held.
    log_session._cleanup(tmp_path, time.time()-72*3600, 'other-launch')
    assert Path(descriptor['log_path']).exists()


def test_resume_does_not_run_cleanup_again(tmp_path, monkeypatch):
    first = logs.start_session(tmp_path, descriptor={})
    resume = logs.make_resume_descriptor()
    logs.close_session()
    def unexpected(*args):
        pytest.fail('resume must not clean history')
    monkeypatch.setattr(log_session, '_cleanup', unexpected)
    resumed = logs.start_session(tmp_path, descriptor=resume)
    assert resumed['log_path'] == first['log_path']
