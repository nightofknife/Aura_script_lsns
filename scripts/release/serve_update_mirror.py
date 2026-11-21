from __future__ import annotations

import argparse
from dataclasses import asdict, dataclass
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import logging
from pathlib import Path
import re
import ssl
from threading import Lock
import time

from packages.aura_core.release_manifest import release_version_key
from packages.aura_core.update_mirror import MAX_REPLY_BYTES, MirrorError, MirrorRelease, parse_mirror_release


CHANNELS = {"stable", "experimental"}
QUEUE_CAPACITY = 512
WAITING_TTL = 60
CLAIM_TTL = 45
LOG = logging.getLogger(__name__)


@dataclass
class Ticket:
    release: MirrorRelease
    expires: float
    ready_until: float | None = None


class DownloadQueue:
    def __init__(self):
        self.waiting: dict[str, Ticket] = {}
        self.active: str | None = None
        self.lock = Lock()

    def _refresh(self, now):
        for token, ticket in list(self.waiting.items()):
            if ticket.expires <= now or (
                ticket.ready_until is not None and ticket.ready_until <= now
            ):
                del self.waiting[token]
        if self.active is None and self.waiting:
            head = next(iter(self.waiting.values()))
            if head.ready_until is None:
                head.ready_until = now + CLAIM_TTL

    def join(self, token, release):
        with self.lock:
            now = time.monotonic()
            self._refresh(now)
            current = self.waiting.get(token)
            if current is not None and current.release == release:
                current.expires = now + WAITING_TTL
                return HTTPStatus.OK
            if current is not None or self.active == token:
                return HTTPStatus.CONFLICT
            if len(self.waiting) >= QUEUE_CAPACITY:
                return HTTPStatus.TOO_MANY_REQUESTS
            self.waiting[token] = Ticket(release, now + WAITING_TTL)
            self._refresh(now)
            return HTTPStatus.OK

    def poll(self, token):
        with self.lock:
            now = time.monotonic()
            self._refresh(now)
            ticket = self.waiting.get(token)
            if ticket is None:
                return None
            ticket.expires = now + WAITING_TTL
            position = list(self.waiting).index(token) + int(self.active is not None)
            return {"state": "ready" if position == 0 else "waiting", "ahead": position}

    def claim(self, token):
        with self.lock:
            self._refresh(time.monotonic())
            if self.active is not None or next(iter(self.waiting), None) != token:
                return None
            self.active = token
            return self.waiting.pop(token).release

    def finish(self, token):
        with self.lock:
            if self.active == token:
                self.active = None
                self._refresh(time.monotonic())

    def cancel(self, token):
        with self.lock:
            self.waiting.pop(token, None)
            self._refresh(time.monotonic())


class MirrorServer(ThreadingHTTPServer):
    def __init__(self, address, directory, *, tls=None):
        self.directory = directory
        self.tls = tls
        self.queue = DownloadQueue()
        super().__init__(address, MirrorHandler)

    def process_request_thread(self, connection, address):
        if self.tls is not None:
            connection.settimeout(15)
            try:
                connection = self.tls.wrap_socket(connection, server_side=True)
            except (OSError, ValueError):
                self.shutdown_request(connection)
                return
        super().process_request_thread(connection, address)

    def asset(self, release):
        return self.directory / "releases" / "download" / release.tag / release.filename

    def latest(self, channel):
        path = self.directory / "channels" / f"{channel}.json"
        with path.open("rb") as handle:
            reply = handle.read(MAX_REPLY_BYTES + 1)
        if len(reply) > MAX_REPLY_BYTES:
            raise MirrorError("Release index exceeds maximum size")
        release = parse_mirror_release(json.loads(reply), channel)
        if self.asset(release).stat().st_size != release.size:
            raise MirrorError("Release index and package differ")
        return release


class MirrorHandler(BaseHTTPRequestHandler):
    def setup(self):
        super().setup()
        self.connection.settimeout(15)

    def log_message(self, format, *args):
        # Queue token URLs must not appear in the request log.
        pass

    def _route(self):
        _, separator, rest = self.path.partition("/")
        route, sub_separator, value = rest.partition("/")
        if separator and sub_separator and "/" not in value and "?" not in value:
            return route, value
        return "", ""

    def _json(self, status, data):
        payload = json.dumps(data).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def do_GET(self):
        route, value = self._route()
        if route == "channels" and value in CHANNELS:
            try:
                release = self.server.latest(value)
            except (MirrorError, OSError, ValueError):
                self.send_error(HTTPStatus.SERVICE_UNAVAILABLE)
                return
            self._json(HTTPStatus.OK, asdict(release))
        elif route == "queue" and re.fullmatch(r"[0-9a-f]{32}", value):
            state = self.server.queue.poll(value)
            if state is None:
                self.send_error(HTTPStatus.NOT_FOUND)
            else:
                self._json(HTTPStatus.OK, state)
        elif route == "download" and re.fullmatch(r"[0-9a-f]{32}", value):
            self._download(value)
        else:
            self.send_error(HTTPStatus.NOT_FOUND)

    def do_PUT(self):
        route, token = self._route()
        if route != "queue" or not re.fullmatch(r"[0-9a-f]{32}", token):
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if not 0 < length <= MAX_REPLY_BYTES:
                raise ValueError("Invalid length")
            payload = json.loads(self.rfile.read(length))
            tag = payload.get("tag") if isinstance(payload, dict) else None
            if not isinstance(tag, str) or len(tag) > 48 or tag != tag.strip():
                raise ValueError("Invalid tag")
            version = release_version_key(tag)
            if version is None:
                raise ValueError("Invalid version")
        except (ValueError, TimeoutError):
            self.send_error(HTTPStatus.BAD_REQUEST)
            return
        try:
            release = self.server.latest("stable" if version.stable else "experimental")
        except (MirrorError, OSError, ValueError):
            self.send_error(HTTPStatus.SERVICE_UNAVAILABLE)
            return
        if release.tag != tag:
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        status = self.server.queue.join(token, release)
        self._json(status, {})

    def do_DELETE(self):
        route, token = self._route()
        if route != "queue" or not re.fullmatch(r"[0-9a-f]{32}", token):
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        self.server.queue.cancel(token)
        self.send_response(HTTPStatus.NO_CONTENT)
        self.end_headers()

    def _download(self, token):
        release = self.server.queue.claim(token)
        if release is None:
            self.send_error(HTTPStatus.CONFLICT)
            return
        start = 0
        remaining = release.size
        try:
            byte_range = re.fullmatch(r"bytes=([0-9]{1,10})-", self.headers.get("Range", ""))
            if self.headers.get("If-Range", release.etag) != release.etag:
                byte_range = None
            if byte_range:
                start = int(byte_range[1])
                if start >= release.size:
                    self.send_response(HTTPStatus.REQUESTED_RANGE_NOT_SATISFIABLE)
                    self.send_header("Content-Range", f"bytes */{release.size}")
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
            remaining = release.size - start
            asset = self.server.asset(release)
            try:
                package = asset.open("rb")
            except OSError:
                self.send_error(HTTPStatus.SERVICE_UNAVAILABLE)
                return
            with package:
                package.seek(start)
                self.send_response(HTTPStatus.PARTIAL_CONTENT if byte_range else HTTPStatus.OK)
                self.send_header("Content-Type", "application/zip")
                self.send_header("Cache-Control", "no-store")
                self.send_header("Accept-Ranges", "bytes")
                self.send_header("ETag", release.etag)
                self.send_header("Content-Length", str(remaining))
                if byte_range:
                    self.send_header("Content-Range", f"bytes {start}-{release.size - 1}/{release.size}")
                self.end_headers()
                # Pin the declared length if the mirror is updated during a transfer.
                while remaining:
                    data = package.read(min(64 * 1024, remaining))
                    if not data:
                        raise EOFError("Mirror package ended before its declared length")
                    self.wfile.write(data)
                    remaining -= len(data)
        except (ConnectionError, TimeoutError, ssl.SSLError, EOFError) as exc:
            LOG.warning("download_interrupted tag=%s offset=%s sent=%s size=%s reason=%s",
                        release.tag, start, release.size - start - remaining, release.size,
                        type(exc).__name__)
        finally:
            self.server.queue.finish(token)


def main():
    parser = argparse.ArgumentParser(description="Serve the Aura CPU update mirror and shared download queue")
    parser.add_argument("--mirror", required=True, type=Path)
    parser.add_argument("--bind", default="127.0.0.1")
    parser.add_argument("--port", default=8443, type=int)
    parser.add_argument("--cert", type=Path)
    parser.add_argument("--key", type=Path)
    args = parser.parse_args()
    if bool(args.cert) != bool(args.key) or (args.cert is None and args.bind != "127.0.0.1"):
        parser.error("A certificate and key are required for a non-loopback listener")
    tls = None
    if args.cert is not None:
        tls = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        tls.load_cert_chain(args.cert, args.key)
    with MirrorServer((args.bind, args.port), args.mirror, tls=tls) as server:
        server.serve_forever()


if __name__ == "__main__":
    main()
