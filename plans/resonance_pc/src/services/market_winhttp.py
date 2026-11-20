"""Windows system-proxy transport for market data requests."""

from __future__ import annotations

import ctypes
import sys
import time
from contextlib import ExitStack
from ctypes import wintypes
from email.parser import Parser
from http.client import responses
from urllib.error import HTTPError
from urllib.parse import urlsplit


_ACCESS_TYPE_AUTOMATIC_PROXY = 4
_FLAG_SECURE = 0x00800000
_QUERY_STATUS_CODE = 19
_QUERY_RAW_HEADERS_CRLF = 22
_QUERY_FLAG_NUMBER = 0x20000000
_ERROR_WINHTTP_TIMEOUT = 12002
_LOAD_LIBRARY_SEARCH_SYSTEM32 = 0x800
_MAX_RESPONSE_BYTES = 16 * 1024 * 1024
_READ_BUFFER_BYTES = 64 * 1024
_MAX_HEADER_BYTES = 128 * 1024


def _load_winhttp():
    # Load the OS copy rather than searching the application's directories.
    dll = ctypes.WinDLL(
        "winhttp.dll", use_last_error=True, winmode=_LOAD_LIBRARY_SEARCH_SYSTEM32
    )
    handle = wintypes.HANDLE
    dword = wintypes.DWORD
    dword_ptr = ctypes.POINTER(dword)
    wide_string = wintypes.LPCWSTR
    buffer_ptr = wintypes.LPVOID
    signatures = (
        ("WinHttpOpen", handle, (wide_string, dword, wide_string, wide_string, dword)),
        ("WinHttpConnect", handle, (handle, wide_string, wintypes.WORD, dword)),
        (
            "WinHttpOpenRequest",
            handle,
            (handle, wide_string, wide_string, wide_string, wide_string,
             ctypes.POINTER(wide_string), dword),
        ),
        (
            "WinHttpSetTimeouts",
            wintypes.BOOL,
            (handle, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int),
        ),
        (
            "WinHttpSendRequest",
            wintypes.BOOL,
            (handle, wide_string, dword, buffer_ptr, dword, dword, ctypes.c_size_t),
        ),
        ("WinHttpReceiveResponse", wintypes.BOOL, (handle, buffer_ptr)),
        ("WinHttpQueryHeaders", wintypes.BOOL, (handle, dword, wide_string, buffer_ptr, dword_ptr, dword_ptr)),
        ("WinHttpReadData", wintypes.BOOL, (handle, buffer_ptr, dword, dword_ptr)),
        ("WinHttpCloseHandle", wintypes.BOOL, (handle,)),
    )
    for name, result_type, argument_types in signatures:
        function = getattr(dll, name)
        function.restype = result_type
        function.argtypes = argument_types
    return dll


def _checked(function, *args):
    result = function(*args)
    if not result:
        error = ctypes.WinError(ctypes.get_last_error())
        message = f"{function.__name__}: {error.strerror}"
        error_type = TimeoutError if error.winerror == _ERROR_WINHTTP_TIMEOUT else OSError
        raise error_type(error.errno, message, None, error.winerror)
    return result


def _set_remaining_timeout(dll, request, deadline: float) -> None:
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise TimeoutError("WinHTTP market request timed out.")
    milliseconds = max(1, min(int(remaining * 1000), 2**31 - 1))
    _checked(
        dll.WinHttpSetTimeouts, request, milliseconds, milliseconds, milliseconds, milliseconds
    )


def _response_headers(dll, request):
    length = wintypes.DWORD()
    dll.WinHttpQueryHeaders(
        request, _QUERY_RAW_HEADERS_CRLF, None, None, ctypes.byref(length), None
    )
    if not 0 < length.value <= _MAX_HEADER_BYTES:
        return None
    buffer = ctypes.create_unicode_buffer(length.value // ctypes.sizeof(ctypes.c_wchar))
    if not dll.WinHttpQueryHeaders(
        request, _QUERY_RAW_HEADERS_CRLF, None, buffer, ctypes.byref(length), None
    ):
        return None
    return Parser().parsestr(buffer.value.partition("\r\n")[2])


def get_market_url(url: str, *, user_agent: str, timeout_seconds: float) -> bytes:
    """GET using the current Windows user's native automatic proxy settings."""
    if sys.platform != "win32":
        raise OSError("Windows automatic proxy is only available on Windows.")

    deadline = time.monotonic() + timeout_seconds
    target = urlsplit(url)
    if target.scheme not in ("http", "https") or not target.hostname or target.username is not None:
        raise ValueError("WinHTTP requires an HTTP(S) URL without embedded credentials.")
    secure = target.scheme == "https"
    port = target.port if target.port is not None else (443 if secure else 80)
    request_path = target.path or "/"
    if target.query:
        request_path += f"?{target.query}"
    hostname = target.hostname.encode("idna").decode("ascii")

    dll = _load_winhttp()
    with ExitStack() as handles:
        session = _checked(dll.WinHttpOpen, user_agent, _ACCESS_TYPE_AUTOMATIC_PROXY, None, None, 0)
        handles.callback(dll.WinHttpCloseHandle, session)
        connection = _checked(dll.WinHttpConnect, session, hostname, port, 0)
        handles.callback(dll.WinHttpCloseHandle, connection)
        request = _checked(
            dll.WinHttpOpenRequest, connection, "GET", request_path,
            None, None, None, _FLAG_SECURE if secure else 0,
        )
        handles.callback(dll.WinHttpCloseHandle, request)

        _set_remaining_timeout(dll, request, deadline)
        _checked(dll.WinHttpSendRequest, request, None, 0, None, 0, 0, 0)
        _set_remaining_timeout(dll, request, deadline)
        _checked(dll.WinHttpReceiveResponse, request, None)

        status = wintypes.DWORD()
        status_size = wintypes.DWORD(ctypes.sizeof(status))
        _checked(
            dll.WinHttpQueryHeaders, request, _QUERY_STATUS_CODE | _QUERY_FLAG_NUMBER,
            None, ctypes.byref(status), ctypes.byref(status_size), None,
        )
        if not 200 <= status.value < 300:
            raise HTTPError(
                url, status.value, responses.get(status.value, "HTTP error"),
                _response_headers(dll, request), None,
            )

        body = bytearray()
        buffer = ctypes.create_string_buffer(_READ_BUFFER_BYTES)
        received = wintypes.DWORD()
        while True:
            _set_remaining_timeout(dll, request, deadline)
            _checked(dll.WinHttpReadData, request, buffer, len(buffer), ctypes.byref(received))
            if not received.value:
                return bytes(body)
            if len(body) + received.value > _MAX_RESPONSE_BYTES:
                raise OSError("WinHTTP market response exceeded the maximum size.")
            body.extend(buffer[:received.value])
