"""Real transport and file boundaries; no game or GUI input."""
import logging
import multiprocessing
import os
from pathlib import Path
import re
import queue
import sys
import threading
import time

import pytest

from packages.aura_core.observability.logging import coordinator as logs
from packages.aura_core.observability.logging.core_logger import logger, set_cid, reset_cid


def _producer(descriptor, number):
    logs.attach_session(descriptor, role=f"worker-{number}")
    token = set_cid(f"child-{number}")
    for index in range(75):
        logger.info("UNIQUE-%s-%s 中文", number, index)
    try:
        raise ValueError("异常完整")
    except ValueError:
        logger.exception("worker-exception-%s", number)
    reset_cid(token)
    assert logs.flush(10)
    logs.close_session()


def _subscriber(descriptor, ready, finished):
    logs.attach_session(descriptor, role="display-worker")
    messages = queue.Queue()
    logger.setup(api_log_queue=messages)
    ready.set()
    deadline = time.monotonic() + 8
    while time.monotonic() < deadline:
        message = messages.get(timeout=8)
        if "HOST_COMMITTED_FOR_REMOTE_UI" in message:
            assert "HOST_COMMITTED_FOR_REMOTE_UI" in Path(descriptor["log_path"]).read_text(encoding="utf-8")
            finished.set()
            break
    assert finished.is_set()
    logs.close_session()


@pytest.fixture
def session(tmp_path, monkeypatch):
    logs.close_session()
    monkeypatch.delenv("AURA_LOG_DESCRIPTOR", raising=False)
    descriptor = logs.start_session(tmp_path / "logs", descriptor={})
    yield descriptor
    logs.close_session()


def test_display_unsubscribe_closes_connection_only_on_receiving_thread(monkeypatch):
    from packages.aura_core.observability.logging import transport

    receiving, unsubscribed = threading.Event(), threading.Event()

    class Connection:
        close_threads = []

        def send_bytes(self, packet):
            if b'"unsubscribe"' in packet:
                unsubscribed.set()

        def close(self):
            self.close_threads.append(threading.current_thread())

    connection = Connection()
    monkeypatch.setattr(transport, "connect", lambda endpoint: connection)

    def receive(connection):
        receiving.set()
        assert unsubscribed.wait(5)
        raise EOFError

    monkeypatch.setattr(transport, "receive", receive)
    subscriber = transport.RemoteSubscriber({}, lambda record: None)
    assert receiving.wait(5)
    subscriber.close()
    subscriber.close()
    subscriber.thread.join(2)
    assert not subscriber.thread.is_alive()
    assert connection.close_threads == [subscriber.thread]


def test_spawn_concurrency_and_restart_share_file_and_preserve_identity(session):
    context = multiprocessing.get_context("spawn")
    for generation in range(2):
        children = [context.Process(target=_producer, args=(session, generation*2+i)) for i in range(2)]
        for child in children:
            child.start()
        for child in children:
            child.join(25)
            if child.is_alive():
                child.terminate()
                child.join(5)
            assert child.exitcode == 0
    logger.info("parent-tail")
    assert logs.flush()
    path = Path(session["log_path"])
    text = path.read_text(encoding="utf-8")
    assert len(list(path.parent.glob("*.log*"))) == 1
    for number in range(4):
        for index in range(75):
            assert text.count(f"UNIQUE-{number}-{index} 中文") == 1
        assert f"[cid:child-{number}]" in text
        assert f"role:worker-{number}" in text
    assert text.count("ValueError: 异常完整") == 4
    assert "parent-tail" in text
    sequence = [int(v) for v in re.findall(r" seq:(\d+)(?: |\])", text)]
    assert sequence == list(range(1, len(sequence)+1))


def test_repeated_setup_and_six_megabytes_do_not_rotate(session):
    path = logs.get_current_log_path()
    logger.setup(log_dir=session["log_root"], task_name="ignored-worker-name")
    logger.setup(log_dir=session["log_root"], task_name="ignored-again")
    logger.info("large-start " + "x" * (6*1024*1024) + " large-end")
    assert logs.flush(10)
    assert logs.get_current_log_path() == path
    assert len(list(Path(path).parent.glob("*.log*"))) == 1
    text = Path(path).read_text(encoding="utf-8")
    assert text.count("large-start") == text.count("large-end") == 1


def test_resume_appends_and_new_launch_creates_new_file(session):
    logger.info("before-handoff")
    resume = logs.make_resume_descriptor()
    assert logs.flush()
    logs.close_session()
    adopted = logs.start_session(session["log_root"], descriptor=resume, cleanup=False, role="helper")
    assert adopted["log_path"] == session["log_path"]
    logger.info("after-handoff")
    assert logs.flush()
    logs.close_session()
    text = Path(session["log_path"]).read_text(encoding="utf-8")
    assert "before-handoff" in text and "after-handoff" in text
    sequence = [int(v) for v in re.findall(r" seq:(\d+)(?: |\])", text)]
    assert sequence == list(range(1, len(sequence)+1))
    newer = logs.start_session(session["log_root"], descriptor={})
    assert newer["session_id"] != session["session_id"]
    assert newer["log_path"] != session["log_path"]


def test_standard_logging_and_capture_preserve_partial_and_exception(session):
    from packages.aura_core.observability.logging.stream_capture import install_capture, uninstall_capture
    import sys
    install_capture()
    try:
        logging.getLogger("ThirdParty").warning("third-party-record")
        sys.stdout.write("partial-中文")
        try:
            raise RuntimeError("unhandled-sample")
        except RuntimeError:
            sys.excepthook(*sys.exc_info())
    finally:
        uninstall_capture()
    assert logs.flush()
    text = Path(session["log_path"]).read_text(encoding="utf-8")
    assert "third-party-record" in text and "partial-中文" in text
    assert "RuntimeError: unhandled-sample" in text


def test_native_stderr_is_drained_without_recursion(session):
    from packages.aura_core.observability.logging.stream_capture import install_capture, uninstall_capture
    install_capture(python_streams=False, native_stderr=True)
    try:
        os.write(2, "native-stderr-中文\n".encode())
    finally:
        uninstall_capture()
    assert logs.flush()
    assert "native-stderr-中文" in Path(session["log_path"]).read_text(encoding="utf-8")


@pytest.mark.skipif(os.name != "nt", reason="Win32 native stderr")
def test_windows_standard_handle_remains_valid_during_and_after_capture(session):
    import ctypes
    from packages.aura_core.observability.logging.stream_capture import install_capture, uninstall_capture
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.GetStdHandle.restype = ctypes.c_void_p
    kernel.WriteFile.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_ulong, ctypes.c_void_p, ctypes.c_void_p]
    install_capture(python_streams=False, native_stderr=True)
    try:
        data = b"WIN32_NATIVE_MARKER\n"
        count = ctypes.c_ulong()
        assert kernel.WriteFile(kernel.GetStdHandle(-12), data, len(data), ctypes.byref(count), None)
        assert count.value == len(data)
    finally:
        uninstall_capture()
    assert logs.flush()
    assert "WIN32_NATIVE_MARKER" in Path(session["log_path"]).read_text(encoding="utf-8")
    assert kernel.WriteFile(kernel.GetStdHandle(-12), b"", 0, ctypes.byref(count), None)


def test_existing_stderr_handler_cannot_feed_captured_logs_back(session):
    from packages.aura_core.observability.logging.stream_capture import install_capture, uninstall_capture
    root = logging.getLogger()
    handler = logging.StreamHandler(sys.stderr)
    root.addHandler(handler)
    install_capture(python_streams=False, native_stderr=True)
    try:
        logging.getLogger('Vendor').warning('SINGLE_EVENT_MARKER')
        time.sleep(.05)
    finally:
        uninstall_capture()
        root.removeHandler(handler)
    assert logs.flush()
    assert Path(session['log_path']).read_text(encoding='utf-8').count('SINGLE_EVENT_MARKER') == 1


def test_invalid_frozen_native_handle_keeps_python_logging_alive(session, monkeypatch):
    from packages.aura_core.observability.logging import stream_capture
    original = os.fstat
    def invalid(fd):
        if fd == 2:
            raise OSError('invalid windowed handle')
        return original(fd)
    monkeypatch.setattr(os, 'fstat', invalid)
    stream_capture.install_capture(native_stderr=True)
    try:
        sys.stderr.write('PYTHON_WITHOUT_NATIVE_HANDLE\n')
    finally:
        stream_capture.uninstall_capture()
    assert logs.flush()
    assert 'PYTHON_WITHOUT_NATIVE_HANDLE' in Path(session['log_path']).read_text(encoding='utf-8')


def test_local_and_remote_displays_only_receive_committed_records(session):
    committed = []
    def consume(record):
        if "LOCAL_COMMITTED_FOR_UI" in record.getMessage():
            committed.append("LOCAL_COMMITTED_FOR_UI" in Path(session["log_path"]).read_text(encoding="utf-8"))
    unsubscribe = logs.subscribe(consume)
    logger.info("LOCAL_COMMITTED_FOR_UI")
    assert logs.flush()
    deadline = time.monotonic() + 3
    while not committed and time.monotonic() < deadline:
        time.sleep(.01)
    assert committed == [True]
    unsubscribe()
    context = multiprocessing.get_context("spawn")
    ready, finished = context.Event(), context.Event()
    child = context.Process(target=_subscriber, args=(session, ready, finished))
    child.start()
    try:
        assert ready.wait(10)
        logger.info("HOST_COMMITTED_FOR_REMOTE_UI")
        assert finished.wait(10)
        child.join(10)
        assert child.exitcode == 0
    finally:
        if child.is_alive():
            child.terminate()
            child.join(5)


def test_stale_descriptor_fails_attachment_without_live_lease(tmp_path):
    logs.close_session()
    descriptor = logs.start_session(tmp_path, descriptor={})
    logs.close_session()
    with pytest.raises(ConnectionError):
        logs.attach_session(descriptor)
    assert logs.get_session_descriptor() is None
    assert not list((tmp_path / '.sessions' / descriptor['session_id']).glob('*.lease'))


def test_writer_error_is_visible_and_flush_reports_failure(session):
    writer = logs._state.producer
    writer.stream.close()
    logger.error("disk-error-probe")
    assert logs.flush() is False
    assert writer.error
    logs.close_session(.2)
    assert not writer.writer.is_alive()


def test_closed_host_console_does_not_break_file_logging(session):
    import io
    from packages.aura_core.observability.logging.core_logger import ConsoleHandler
    output = io.StringIO()
    handler = ConsoleHandler(output)
    logger.logger.addHandler(handler)
    output.close()
    try:
        logger.info('HOST_CONSOLE_CLOSED_BUT_FILE_ALIVE')
        assert logs.flush()
    finally:
        logger.logger.removeHandler(handler)
    assert 'HOST_CONSOLE_CLOSED_BUT_FILE_ALIVE' in Path(session['log_path']).read_text(encoding='utf-8')


def test_native_mixed_encodings_and_split_utf16_do_not_make_binary_logs(session):
    from packages.aura_core.observability.logging.stream_capture import forward_stream
    payload = ("[W:onnxruntime:] 原生告警😀\n".encode('utf-16-le')
               + 'UTF8 中文尾部\n'.encode('utf-8')
               + 'WIDE 无换行尾部'.encode('utf-16-le'))
    class Pipe:
        def __init__(self):
            self.offset = 0
        def read(self, size):
            value = payload[self.offset:self.offset+3]
            self.offset += len(value)
            return value
    forward_stream(Pipe())
    assert logs.flush()
    raw = Path(session['log_path']).read_bytes()
    text = raw.decode('utf-8')
    assert b'\0' not in raw
    for expected in ['[W:onnxruntime:] 原生告警😀', 'UTF8 中文尾部', 'WIDE 无换行尾部']:
        assert text.count(expected) == 1
