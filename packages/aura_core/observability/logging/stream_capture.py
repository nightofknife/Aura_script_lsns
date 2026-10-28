"""Application-owned capture adapters; protocol stdout is always opt-out."""
from __future__ import annotations

import asyncio
import codecs
import logging
import os
import sys
import threading

from .transport import emergency, set_emergency_stream

_lock = threading.RLock()
_installed = False
_saved = {}
_native = None
_qt_handler = None
_loop = None


def _emit(name, text, process_id=None):
    if not text:
        return
    log = logging.getLogger("AuraCaptured." + name)
    extra = {"source_pid": process_id} if process_id is not None else {}
    log.log(logging.ERROR if name.endswith("stderr") else logging.INFO, text.rstrip("\r\n"), extra=extra)


class CapturedStream:
    def __init__(self, original, name):
        self.original, self.name = original, name
        self.pending = ""
        self.lock = threading.RLock()
        self.encoding = getattr(original, "encoding", None) or "utf-8"

    def write(self, value):
        value = str(value)
        with self.lock:
            self.pending += value
            while "\n" in self.pending or len(self.pending) >= 8192:
                end = self.pending.find("\n") + 1
                if not end:
                    end = 8192
                line, self.pending = self.pending[:end], self.pending[end:]
                _emit(self.name, line)
        return len(value)

    def flush(self):
        with self.lock:
            if self.pending:
                _emit(self.name, self.pending)
                self.pending = ""

    def isatty(self):
        return False

    def fileno(self):
        if self.original is None:
            raise OSError("No original stream descriptor")
        return self.original.fileno()


def forward_stream(stream, *, name="stderr", process_id=None):
    """Drain UTF-8/Windows wide-character lines without corrupting split glyphs.

    Native ONNX writes UTF-16LE even when Python uses UTF-8. Detect each new
    line, so another native library can use UTF-8 on the very same handle.
    """
    pending = bytearray()
    encoding = None
    decoder = None

    def drain(final=False):
        nonlocal encoding, decoder
        while pending:
            if encoding is None:
                if len(pending) < 4 and not final:
                    return
                if pending[:2] in (b"\xff\xfe", b"\xfe\xff"):
                    encoding = "utf-16-le" if pending[:2] == b"\xff\xfe" else "utf-16-be"
                    del pending[:2]
                elif len(pending) >= 4 and pending[1] == 0 and pending[3] == 0:
                    encoding = "utf-16-le"
                else:
                    encoding = "utf-8"
                decoder = codecs.getincrementaldecoder(encoding)(errors="replace")
            wide = encoding.startswith("utf-16")
            newline = b"\x00\n" if encoding == "utf-16-be" else (b"\n\x00" if wide else b"\n")
            position = pending.find(newline)
            while wide and position >= 0 and position % 2:
                position = pending.find(newline, position + 1)
            complete = position >= 0
            if complete:
                end = position + len(newline)
            elif len(pending) >= 8192:
                end = 8192
            elif final:
                end = len(pending)
            else:
                return
            data = bytes(pending[:end])
            del pending[:end]
            text = decoder.decode(data, final=complete or (final and not pending))
            _emit(name, text, process_id)
            if complete:
                encoding = decoder = None
        if final and decoder is not None:
            _emit(name, decoder.decode(b"", final=True), process_id)
            encoding = decoder = None

    try:
        while True:
            chunk = stream.read(4096)
            if not chunk:
                break
            if isinstance(chunk, str):
                drain(final=True)
                _emit(name, chunk, process_id)
            else:
                pending.extend(chunk)
                drain()
        drain(final=True)
    except (OSError, ValueError) as exc:
        emergency(f"Output capture ended: {exc}")


def install_asyncio_capture(loop=None):
    global _loop
    try:
        loop = loop or asyncio.get_running_loop()
    except RuntimeError:
        return
    if _loop is loop:
        return
    previous = loop.get_exception_handler()
    def handler(loop, context):
        error = context.get("exception")
        logging.getLogger("AuraCaptured.asyncio").error(
            context.get("message", "Unhandled asyncio exception"),
            exc_info=(type(error), error, error.__traceback__) if error else None)
    loop.set_exception_handler(handler)
    _saved["asyncio"] = (loop, previous)
    _loop = loop


def install_capture(*, python_streams=True, native_stderr=False, qt=False, capture_stdout=True):
    global _installed, _qt_handler, _native
    with _lock:
        if not _installed:
            # Existing console handlers would echo captured native stderr back
            # into the same pipe. Keep file logging authoritative, restore the
            # host's handlers when capture ends.
            removed = []
            for target in [logging.getLogger(), *[
                item for item in logging.Logger.manager.loggerDict.values() if isinstance(item, logging.Logger)
            ]]:
                for handler in list(target.handlers):
                    if isinstance(handler, logging.StreamHandler) and not isinstance(handler, logging.FileHandler):
                        if target.name == "AuraFramework" and handler.name == "console":
                            continue
                        target.removeHandler(handler)
                        removed.append((target, handler))
            _saved["console_handlers"] = removed
            _saved["excepthook"] = sys.excepthook
            _saved["threading"] = threading.excepthook
            def exception(kind, value, tb):
                logging.getLogger("AuraCaptured.exception").critical("Unhandled exception", exc_info=(kind, value, tb))
                from .coordinator import flush
                flush(2)
            def thread_exception(args):
                exception(args.exc_type, args.exc_value, args.exc_traceback)
            sys.excepthook = exception
            threading.excepthook = thread_exception
            logging.captureWarnings(True)
            if python_streams:
                _saved["stderr"] = sys.stderr
                sys.stderr = CapturedStream(sys.stderr, "stderr")
                if capture_stdout:
                    _saved["stdout"] = sys.stdout
                    sys.stdout = CapturedStream(sys.stdout, "stdout")
            _installed = True
        install_asyncio_capture()
        if qt and _qt_handler is None:
            from PySide6.QtCore import qInstallMessageHandler
            def qt_message(kind, context, message):
                level = logging.ERROR if int(kind.value) in (2, 3) else logging.WARNING if int(kind.value) == 1 else logging.INFO
                logging.getLogger("AuraCaptured.qt").log(level, message)
            _saved["qt"] = qInstallMessageHandler(qt_message)
            _qt_handler = qt_message
        if native_stderr and _native is None:
            # The raw fd/HANDLE is captured as well as Python sys.stderr.
            # Native diagnostics must not write to the same stream used to
            # report logging failure, otherwise a failed writer could recurse.
            try:
                # Windowed frozen workers can have a CRT fd entry whose native
                # handle is invalid: dup() alone does not reliably reject it.
                os.fstat(2)
                saved_fd = os.dup(2)
            except OSError:
                logging.getLogger("AuraCaptured.native").warning(
                    "Native stderr fd is unavailable; launcher pipe and Python exception capture remain active")
                return
            emergency_fd = os.dup(saved_fd)
            try:
                emergency_output = os.fdopen(emergency_fd, "w", encoding="utf-8", errors="replace")
            except OSError:
                os.close(emergency_fd)
                os.close(saved_fd)
                logging.getLogger("AuraCaptured.native").warning("Native stderr handle is unavailable")
                return
            set_emergency_stream(emergency_output)
            read_fd, write_fd = os.pipe()
            os.dup2(write_fd, 2)
            saved_handle = None
            if os.name == "nt":
                import ctypes
                import msvcrt
                kernel = ctypes.WinDLL("kernel32", use_last_error=True)
                kernel.GetStdHandle.restype = ctypes.c_void_p
                kernel.SetStdHandle.argtypes = [ctypes.c_ulong, ctypes.c_void_p]
                saved_handle = kernel.GetStdHandle(-12)
                # dup2 owns a distinct handle: the temporary pipe descriptor is
                # closed below, so publishing its handle would immediately make
                # GetStdHandle(STD_ERROR_HANDLE) invalid.
                kernel.SetStdHandle(-12 & 0xffffffff, msvcrt.get_osfhandle(2))
            os.close(write_fd)
            reader = os.fdopen(read_fd, "rb", buffering=0)
            thread = threading.Thread(target=forward_stream, args=(reader,),
                                      name="aura-native-stderr", daemon=True)
            _native = (saved_fd, saved_handle, reader, thread, emergency_output)
            thread.start()


def uninstall_capture():
    global _installed, _qt_handler, _native, _loop
    with _lock:
        if _native is not None:
            fd, handle, reader, thread, emergency_output = _native
            os.dup2(fd, 2)
            os.close(fd)
            if os.name == "nt" and handle is not None:
                import ctypes
                import msvcrt
                kernel = ctypes.WinDLL("kernel32", use_last_error=True)
                kernel.SetStdHandle.argtypes = [ctypes.c_ulong, ctypes.c_void_p]
                kernel.SetStdHandle(-12 & 0xffffffff, msvcrt.get_osfhandle(2))
            thread.join(2)
            if not thread.is_alive():
                reader.close()
            set_emergency_stream(None)
            emergency_output.close()
            _native = None
        for name in ("stdout", "stderr"):
            if name in _saved:
                current = getattr(sys, name)
                if isinstance(current, CapturedStream):
                    current.flush()
                setattr(sys, name, _saved.pop(name))
        if "excepthook" in _saved:
            sys.excepthook = _saved.pop("excepthook")
            threading.excepthook = _saved.pop("threading")
        if "asyncio" in _saved:
            loop, previous = _saved.pop("asyncio")
            if not loop.is_closed():
                loop.set_exception_handler(previous)
        if _qt_handler is not None:
            from PySide6.QtCore import qInstallMessageHandler
            qInstallMessageHandler(_saved.pop("qt"))
            _qt_handler = None
        logging.captureWarnings(False)
        for target, handler in _saved.pop("console_handlers", []):
            target.addHandler(handler)
        _installed = False
        _loop = None
