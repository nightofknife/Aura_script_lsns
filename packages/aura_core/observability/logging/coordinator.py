"""One application session, one file writer; lightweight enough for launchers."""
from __future__ import annotations

import atexit
import copy
import json
import logging
import os
from pathlib import Path
import queue
import re
import threading
import time
from uuid import uuid4

from .log_session import (_FileLock, get_log_session, resolve_log_root,
                          safe_managed_path, start_log_session, flush_session_diagnostics)
from .transport import QUEUE_SIZE, RemoteProducer, RemoteSubscriber, connect, emergency, encode, make_listener, receive

_lock = threading.RLock()
_state = None
_ENV = "AURA_LOG_DESCRIPTOR"


class SessionFormatter(logging.Formatter):
    def format(self, record):
        original = record.msg
        record.msg = (f"[session:{record.session_id} pid:{record.process} role:{record.role} "
                      f"root:{record.root_cid} seq:{record.sequence} thread:{record.thread} "
                      f"instance:{record.process_instance_id}] {original}")
        try:
            return super().format(record)
        finally:
            record.msg = original


def _snapshot(record, descriptor, role, instance):
    from .core_logger import current_cid
    root_cid = getattr(record, "root_cid", None)
    # Do not import the execution tracing module during early bootstrap.
    import sys
    trace = sys.modules.get("packages.aura_core.observability.runtime_trace")
    if root_cid is None and trace is not None:
        root_cid = trace.current_root_cid()
    if root_cid in (None, "", "-"):
        root_cid = getattr(record, "cid", current_cid())
    fields = {key: getattr(record, key, None) for key in (
        "name", "levelno", "levelname", "pathname", "lineno", "funcName", "created",
        "msecs", "thread", "threadName", "process", "processName", "stack_info", "module", "filename",
    )}
    fields.update(msg=record.getMessage(), args=(),
                  cid=getattr(record, "cid", current_cid()),
                  root_cid=root_cid or getattr(record, "cid", current_cid()),
                  session_id=descriptor["session_id"], role=role, process_instance_id=instance,
                  exc_text=(logging.Formatter().formatException(record.exc_info)
                            if record.exc_info else record.exc_text),
                  exc_info=None)
    extra = getattr(record, "extra_fields", None)
    if extra:
        fields["msg"] += " " + json.dumps(extra, ensure_ascii=False, default=str)
    if hasattr(record, "source_pid"):
        fields["msg"] = f"[source_pid:{record.source_pid}] " + fields["msg"]
    for key in ("gui_display_id", "gui_display_message"):
        if hasattr(record, key):
            fields[key] = getattr(record, key)
    return fields


class ProducerHandler(logging.Handler):
    def __init__(self, state):
        super().__init__(logging.DEBUG)
        self.state = state
        self.set_name("task_file")  # compatibility identity, never a FileHandler

    def emit(self, record):
        try:
            self.state.producer.submit(_snapshot(record, self.state.descriptor,
                                                  self.state.role, self.state.instance))
        except Exception as exc:
            emergency(f"Could not enqueue log record: {exc}")

    def flush(self):
        if getattr(self, "state", None) is not None and not self.state.closed:
            self.state.producer.flush(2)


class LocalWriter:
    def __init__(self, path, session_id):
        self.path = path
        self.session_id = session_id
        self.instance = f"{os.getpid()}-{uuid4().hex[:12]}"
        self.queue = queue.Queue(maxsize=QUEUE_SIZE)
        self.error = None
        self.closed = False
        self.dropped = 0
        self.sequence = 0
        if path.exists():
            with path.open("rb") as previous:
                previous.seek(max(0, path.stat().st_size - 65536))
                tail = previous.read().decode("utf-8", errors="replace")
            numbers = re.findall(r"^\d{4}-\d\d-\d\d .*?\[session:[^\n]*? seq:(\d+)(?: |\])", tail, re.MULTILINE)
            if numbers:
                self.sequence = max(map(int, numbers))
        self.connections = set()
        self.connections_lock = threading.Lock()
        self.receivers = []
        self.accept_stopping = threading.Event()
        self.stopping = threading.Event()
        self.subscribers = []
        self.display_queue = queue.Queue(maxsize=QUEUE_SIZE)
        self.stream = path.open("a", encoding="utf-8", newline="\n")
        self.formatter = SessionFormatter(
            '%(asctime)s - %(levelname)-8s - [cid:%(cid)s] - %(name)s - '
            '%(module)s.%(funcName)s:%(lineno)d - %(message)s')
        try:
            self.listener, self.endpoint = make_listener()
        except Exception:
            self.stream.close()
            raise
        self.writer = threading.Thread(target=self._write, name="aura-log-writer", daemon=True)
        self.acceptor = threading.Thread(target=self._accept, name="aura-log-listener", daemon=True)
        self.display = threading.Thread(target=self._display, name="aura-log-display", daemon=True)
        self.writer.start()
        self.acceptor.start()
        self.display.start()

    def submit(self, record):
        if self.closed or self.error:
            emergency("Writer unavailable: " + str(self.error or "closed"))
            return False
        try:
            self.queue.put_nowait((record, None))
            return True
        except queue.Full:
            self.dropped += 1
            emergency("Writer queue full; log record lost")
            return False

    def _write(self):
        while True:
            record, barrier = self.queue.get()
            try:
                if self.dropped:
                    count, self.dropped = self.dropped, 0
                    warning = logging.LogRecord("AuraLogTransport", logging.ERROR, __file__, 0,
                                                "Logging overflow: dropped_records=%s", (count,), None)
                    self._commit(_snapshot(warning, {"session_id": self.session_id}, "writer", self.instance))
                if record is None:
                    self.stream.flush()
                    if barrier is None:
                        break
                else:
                    self._commit(record)
            except Exception as exc:
                self.error = str(exc)
                emergency("File write failed: " + self.error)
            finally:
                if barrier is not None:
                    barrier.set()
            if record is None and barrier is None:
                break  # Even a failed flush must consume the stop sentinel.
        self.stream.close()

    def _commit(self, record):
        self.sequence += 1
        record["sequence"] = self.sequence
        event = logging.makeLogRecord(record)
        self.stream.write(self.formatter.format(event) + "\n")
        self.stream.flush()
        try:
            self.display_queue.put_nowait(event)
        except queue.Full:
            pass  # Display loss never affects the already written file.

    def _display(self):
        while True:
            record = self.display_queue.get()
            if record is None:
                break
            for callback in list(self.subscribers):
                try:
                    callback(record)
                except Exception as exc:
                    emergency(f"Log display subscriber failed: {exc}")

    def _accept(self):
        while not self.accept_stopping.is_set():
            try:
                connection = self.listener.accept()
            except (OSError, EOFError):
                break
            except Exception as exc:
                emergency(f"Log connection rejected: {exc}")
                continue
            if self.accept_stopping.is_set():
                connection.close()
                break
            with self.connections_lock:
                self.connections.add(connection)
            thread = threading.Thread(target=self._receive, args=(connection,),
                                      name="aura-log-client", daemon=True)
            self.receivers.append(thread)
            thread.start()

    def _receive(self, connection):
        from multiprocessing.connection import deliver_challenge, answer_challenge
        authentication_timeout = threading.Timer(3, connection.close)
        authentication_timeout.daemon = True
        try:
            authentication_timeout.start()
            key = bytes.fromhex(self.endpoint["authkey"])
            deliver_challenge(connection, key)
            answer_challenge(connection, key)
            authentication_timeout.cancel()
            while not self.stopping.is_set():
                packet = receive(connection)
                if packet.get("op") == "record":
                    if not isinstance(packet.get("record"), dict):
                        raise ValueError("Invalid record")
                    # A slow file writer backpressures this transport thread only.
                    self.queue.put((packet["record"], None), timeout=2)
                elif packet.get("op") == "flush":
                    ok = self.flush(5)
                    connection.send_bytes(encode({"ok": ok, "error": self.error}))
                elif packet.get("op") == "overflow":
                    lost = max(0, int(packet.get("count", 0)))
                    self.dropped += lost
                    emergency(f"Producer pid={packet.get('pid')} lost {lost} log records")
                elif packet.get("op") == "subscribe":
                    self._serve_subscription(connection)
                    return
                else:
                    raise ValueError("Unknown log operation")
        except (EOFError, BrokenPipeError, OSError):
            pass
        except Exception as exc:
            emergency(f"Log receive failed: {exc}")
        finally:
            authentication_timeout.cancel()
            with self.connections_lock:
                self.connections.discard(connection)
            connection.close()

    def _serve_subscription(self, connection):
        pending = queue.Queue(maxsize=2048)
        def enqueue(record):
            try:
                pending.put_nowait(dict(record.__dict__))
            except queue.Full:
                pass  # Only this display consumer can miss records.
        self.subscribers.append(enqueue)
        try:
            connection.send_bytes(encode({"op": "subscribed"}))
            while not self.accept_stopping.is_set():
                if connection.poll(0):
                    if receive(connection).get("op") == "unsubscribe":
                        break
                try:
                    record = pending.get(timeout=.2)
                except queue.Empty:
                    continue
                connection.send_bytes(encode({"op": "written", "record": record}))
        finally:
            self.subscribers.remove(enqueue)

    def flush(self, timeout=5):
        if self.closed:
            return self.error is None
        deadline = time.monotonic() + timeout
        barrier = threading.Event()
        try:
            self.queue.put((None, barrier), timeout=max(0, timeout))
        except queue.Full:
            return False
        return barrier.wait(max(0, deadline-time.monotonic())) and self.error is None

    def close(self, timeout=5):
        if self.closed:
            return
        self.accept_stopping.set()
        # Wake a blocking Windows named-pipe accept without blocking shutdown.
        def wake():
            try:
                connect(self.endpoint).close()
            except Exception:
                pass
        threading.Thread(target=wake, daemon=True).start()
        self.acceptor.join(min(1, timeout))
        self.listener.close()
        deadline = time.monotonic() + timeout
        for thread in list(self.receivers):
            thread.join(max(0, deadline - time.monotonic()))
        self.stopping.set()
        with self.connections_lock:
            for connection in list(self.connections):
                try:
                    connection.close()
                except OSError:
                    pass
        for thread in list(self.receivers):
            thread.join(.1)
        if any(thread.is_alive() for thread in self.receivers):
            emergency("Log receiver did not drain before shutdown deadline")
        if not self.flush(timeout):
            emergency("Writer shutdown could not drain all records")
        self.closed = True
        try:
            self.queue.put((None, None), timeout=timeout)
        except queue.Full:
            emergency("Writer shutdown queue remained full")
        self.writer.join(timeout)
        try:
            self.display_queue.put_nowait(None)
        except queue.Full:
            pass


class _State:
    def __init__(self, descriptor, producer, role, session, writer_lock=None):
        self.descriptor, self.producer, self.role = descriptor, producer, role
        self.session, self.writer_lock = session, writer_lock
        self.instance = f"{os.getpid()}-{uuid4().hex[:12]}"
        self.pid = os.getpid()
        self.closed = False
        self.handler = ProducerHandler(self)
        self.display_handlers = {}
        self.callbacks = []
        self.subscription = None

    def display(self, record):
        if isinstance(record, dict):
            record = logging.makeLogRecord(record)
        for handler in list(self.display_handlers.values()):
            if record.levelno >= handler.level:
                try:
                    handler.handle(record)
                except Exception as exc:
                    emergency(f"Display handler failed: {exc}")
        for callback in list(self.callbacks):
            try:
                callback(record)
            except Exception as exc:
                emergency(f"Display callback failed: {exc}")


def _install(state):
    from .core_logger import logger
    for handler in list(logger.logger.handlers):
        if handler.name == "console" and getattr(handler, "stream", None) is None:
            logger.logger.removeHandler(handler)
        if handler.name == "task_file":
            logger.logger.removeHandler(handler)
            handler.close()
    logger.logger.addHandler(state.handler)
    root = logging.getLogger()
    state.root_level = root.level
    root.setLevel(logging.DEBUG)
    root.addHandler(state.handler)
    captured = logging.getLogger("AuraCaptured")
    state.captured_settings = (captured.level, captured.propagate)
    captured.setLevel(logging.DEBUG)
    captured.propagate = False
    captured.addHandler(state.handler)
    for handler in list(logger.logger.handlers):
        if handler.name == "bootstrap":
            logger.logger.removeHandler(handler)
            for record in handler.records:
                state.handler.handle(record)
    flush_session_diagnostics(logger.logger)
    # LogSession registers lease cleanup when it starts. Move our shutdown to
    # the front of the atexit stack so the writer always closes before leases.
    atexit.unregister(close_session)
    atexit.register(close_session)


def get_display_handler(name):
    return _state.display_handlers.get(name) if get_session_descriptor() else None


def route_display_handlers():
    """Move existing UI handlers onto the committed-record subscription."""
    if not get_session_descriptor():
        return
    from .core_logger import logger
    state = _state
    for handler in list(logger.logger.handlers):
        if handler.name in {"api_queue", "ui_queue"}:
            logger.logger.removeHandler(handler)
            state.display_handlers[handler.name] = handler
    _ensure_display_subscription(state)


def _ensure_display_subscription(state):
    if (state.display_handlers or state.callbacks) and state.subscription is None:
        if isinstance(state.producer, LocalWriter):
            state.producer.subscribers.append(state.display)
            state.subscription = True
        else:
            state.subscription = RemoteSubscriber(state.descriptor["transport"], state.display)


def get_session_descriptor():
    if _state is not None and _state.pid == os.getpid() and not _state.closed:
        return copy.deepcopy(_state.descriptor)
    return None


def get_current_log_path():
    descriptor = get_session_descriptor()
    return descriptor["log_path"] if descriptor else None


def session_environment(descriptor=None):
    descriptor = descriptor or get_session_descriptor()
    if not descriptor:
        return {}
    return {_ENV: json.dumps(descriptor), "AURA_LOG_SESSION_ID": descriptor["session_id"],
            "AURA_LOG_ROOT": descriptor["log_root"]}


def make_resume_descriptor():
    descriptor = get_session_descriptor()
    if descriptor is None:
        raise RuntimeError("No log session to hand off")
    descriptor.pop("transport", None)
    descriptor["resume"] = True
    return descriptor


def start_session(log_root, *, cleanup=True, descriptor=None, role="host"):
    global _state
    with _lock:
        current = get_session_descriptor()
        if current is not None:
            return current
        if descriptor is None and os.environ.get(_ENV):
            descriptor = json.loads(os.environ[_ENV])
        if descriptor and descriptor.get("transport"):
            return attach_session(descriptor, role=role)
        root = resolve_log_root(Path.cwd(), log_root)
        resume = bool(descriptor and descriptor.get("resume"))
        existing = get_log_session()
        session_id = descriptor["session_id"] if resume else (
            existing.session_id if existing and existing.root == root
            else time.strftime("%Y%m%d-%H%M%S") + "-" + uuid4().hex[:8])
        session = start_log_session(root, cleanup=cleanup and not resume, session_id=session_id)
        if session.root != root or session.session_id != session_id:
            raise RuntimeError("Conflicting active log session")
        name = Path(descriptor["log_path"]).name if resume else f"aura_session_{time.strftime('%Y%m%d-%H%M%S')}_{session_id}.log"
        path = safe_managed_path(root, name)
        if resume and Path(descriptor["log_path"]).absolute() != path:
            raise ValueError("Resume log path does not belong to session root")
        lock_path = root / ".sessions" / session_id / "writer.lock"
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        writer_lock = _FileLock(lock_path)
        if not writer_lock.acquire():
            session.close()
            raise RuntimeError("This log session already has a writer")
        try:
            if not resume:
                with path.open("x", encoding="utf-8"):
                    pass
            session.register_log(path)
            producer = LocalWriter(path, session_id)
        except Exception:
            writer_lock.close()
            session.close()
            raise
        description = {"version": 1, "session_id": session_id, "log_root": str(root),
                       "log_path": str(path), "transport": producer.endpoint}
        _state = _State(description, producer, role, session, writer_lock)
        os.environ.update(session_environment(description))
        _install(_state)
        from .core_logger import logger
        logger.info("[LogSession] started role=%s log_file=%s resumed=%s", role, path, resume)
        return copy.deepcopy(description)


def attach_session(descriptor, *, role="worker"):
    global _state
    with _lock:
        current = get_session_descriptor()
        if current is not None:
            if current["session_id"] != descriptor["session_id"]:
                raise RuntimeError("Cannot attach to a different active log session")
            return current
        if descriptor.get("version") != 1 or not descriptor.get("transport"):
            raise ValueError("Invalid log session descriptor")
        root = resolve_log_root(Path.cwd(), descriptor["log_root"])
        path = safe_managed_path(root, Path(descriptor["log_path"]).name)
        if str(path) != descriptor["log_path"]:
            raise ValueError("Log path is outside session root")
        session = start_log_session(root, cleanup=False, session_id=descriptor["session_id"])
        if session.root != root or session.session_id != descriptor["session_id"]:
            raise RuntimeError("Conflicting local evidence/log session")
        session.register_log(path)
        producer = RemoteProducer(descriptor["transport"])
        if not producer.flush(5):
            producer.close(.1)
            session.close()
            raise ConnectionError("Cannot attach to the application log writer")
        _state = _State(copy.deepcopy(descriptor), producer, role, session)
        os.environ.update(session_environment(descriptor))
        _install(_state)
        from .core_logger import logger
        logger.info("[LogSession] attached role=%s", role)
        return get_session_descriptor()


def subscribe(callback):
    """Subscribe local/remote UI to committed records outside the writer thread."""
    if not get_session_descriptor():
        raise RuntimeError("No active session")
    state = _state
    state.callbacks.append(callback)
    _ensure_display_subscription(state)
    def unsubscribe():
        if callback in state.callbacks:
            state.callbacks.remove(callback)
    return unsubscribe


def flush(timeout=5):
    return _state.producer.flush(timeout) if get_session_descriptor() else True


def close_session(timeout=5):
    global _state
    with _lock:
        state = _state
        if state is None or state.closed or state.pid != os.getpid():
            return
        from .stream_capture import uninstall_capture
        uninstall_capture()
        from .core_logger import logger
        logger.info("[LogSession] leaving role=%s", state.role)
        logger.logger.removeHandler(state.handler)
        logging.getLogger().removeHandler(state.handler)
        logging.getLogger().setLevel(state.root_level)
        captured = logging.getLogger("AuraCaptured")
        captured.removeHandler(state.handler)
        captured.setLevel(state.captured_settings[0])
        captured.propagate = state.captured_settings[1]
        if isinstance(state.subscription, RemoteSubscriber):
            state.subscription.close()
        state.producer.close(timeout)
        if isinstance(state.producer, LocalWriter) and state.producer.writer.is_alive():
            emergency("Writer is still active; retaining its exclusive lock and session lease")
            return
        state.closed = True
        if state.writer_lock is not None:
            state.writer_lock.close()
        state.session.close()
        _state = None
        for key, value in session_environment(state.descriptor).items():
            if os.environ.get(key) == value:
                os.environ.pop(key, None)


atexit.register(close_session)
