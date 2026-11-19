from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
import http.client
import json
import re
import ssl
import time
from urllib import error, parse, request
import uuid

from packages.aura_core.release_manifest import release_version_key


MIRROR_URL_ENV = "AURA_UPDATE_VPS_URL"
MIRROR_CA_FILE_ENV = "AURA_UPDATE_VPS_CA_FILE"
MAX_MIRROR_ASSET_BYTES = 2 * 1024 * 1024 * 1024
MAX_REPLY_BYTES = 4096
NETWORK_ERRORS = (error.URLError, ConnectionError, TimeoutError, http.client.HTTPException, ssl.SSLError)


class MirrorError(RuntimeError):
    def __init__(self, message: str, *, status: int | None = None):
        super().__init__(message)
        self.status = status


@dataclass(frozen=True)
class MirrorRelease:
    tag: str
    size: int
    sha256: str

    @property
    def filename(self) -> str:
        return f"AuraResonance-{self.tag}-win-x64-cpu.zip"


def parse_mirror_release(payload: object, channel: str) -> MirrorRelease:
    if not isinstance(payload, dict):
        raise MirrorError("VPS 版本索引格式无效")
    tag, size, digest = payload.get("tag"), payload.get("size"), payload.get("sha256")
    if not isinstance(tag, str) or len(tag) > 48 or tag != tag.strip():
        raise MirrorError("VPS 版本索引的标签无效")
    version = release_version_key(tag)
    if version is None or version.stable != (channel == "stable"):
        raise MirrorError("VPS 版本与所选通道不符")
    if type(size) is not int or not 0 < size <= MAX_MIRROR_ASSET_BYTES:
        raise MirrorError("VPS 版本索引的文件大小无效")
    if not isinstance(digest, str) or re.fullmatch(r"[0-9a-fA-F]{64}", digest) is None:
        raise MirrorError("VPS 版本索引的 SHA-256 无效")
    return MirrorRelease(tag, size, digest.lower())


class _RejectRedirects(request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class MirrorClient:
    def __init__(self, base_url: str, *, opener=None, ca_file=None, poll_interval: float = 3.0):
        try:
            address = parse.urlsplit(base_url)
            port = address.port
        except ValueError:
            raise MirrorError("VPS 更新地址无效") from None
        if (address.scheme != "https" or not address.hostname or port == 0
                or address.username is not None or address.password is not None
                or address.path not in {"", "/"} or address.query or address.fragment):
            raise MirrorError("VPS 更新地址必须是 HTTPS 站点地址")
        self.base_url = base_url.rstrip("/")
        if opener is None:
            handlers = [_RejectRedirects()]
            if ca_file is not None:
                try:
                    context = ssl.create_default_context(cafile=str(ca_file))
                except (OSError, ValueError):
                    raise MirrorError("VPS 专用证书无法读取") from None
                handlers.append(request.HTTPSHandler(context=context))
            opener = request.build_opener(*handlers).open
        self.open = opener
        self.poll_interval = poll_interval

    def _request(self, path, *, method="GET", payload=None, timeout=8.0, attempts=3):
        headers = {"User-Agent": "AuraResonanceUpdater/1"}
        body = None
        if payload is not None:
            headers["Content-Type"] = "application/json"
            body = json.dumps(payload).encode("utf-8")
        req = request.Request(self.base_url + path, data=body, headers=headers, method=method)
        for attempt in range(attempts):
            try:
                return self.open(req, timeout=timeout)
            except error.HTTPError as exc:
                status = exc.code
                exc.close()
                if status < 500 or attempt == attempts - 1:
                    raise MirrorError(f"VPS 返回 HTTP {status}", status=status) from None
            except NETWORK_ERRORS as exc:
                if attempt == attempts - 1:
                    raise MirrorError(f"VPS 网络请求失败（{type(exc).__name__}）") from None
            time.sleep(0.5 * (attempt + 1))

    def _json(self, path, *, method="GET", payload=None):
        try:
            with self._request(path, method=method, payload=payload) as response:
                contents = response.read(MAX_REPLY_BYTES + 1)
        except NETWORK_ERRORS as exc:
            raise MirrorError(f"VPS 响应读取失败（{type(exc).__name__}）") from None
        if len(contents) > MAX_REPLY_BYTES:
            raise MirrorError("VPS 响应过大")
        try:
            return json.loads(contents)
        except (ValueError, UnicodeError):
            raise MirrorError("VPS 响应格式无效") from None

    def latest(self, channel: str) -> MirrorRelease:
        return parse_mirror_release(self._json(f"/channels/{channel}"), channel)

    def _join_and_wait(self, tag, queue_path, on_wait):
        previous = None
        while True:
            try:
                self._json(queue_path, method="PUT", payload={"tag": tag})
                break
            except MirrorError as exc:
                if exc.status != 429:
                    raise
                if previous is None:
                    on_wait(None)
                    previous = "full"
                time.sleep(self.poll_interval)
        while True:
            reply = self._json(queue_path)
            if not isinstance(reply, dict):
                raise MirrorError("VPS 排队状态格式无效")
            state, ahead = reply.get("state"), reply.get("ahead")
            if state == "ready" and type(ahead) is int and ahead == 0:
                return
            if state != "waiting" or type(ahead) is not int or ahead < 1:
                raise MirrorError("VPS 排队位置无效")
            if ahead != previous:
                on_wait(ahead)
                previous = ahead
            time.sleep(self.poll_interval)

    @contextmanager
    def stream(self, release: MirrorRelease, *, on_wait, on_start, timeout):
        for attempt in range(2):
            token = uuid.uuid4().hex
            queue_path = f"/queue/{token}"
            try:
                try:
                    self._join_and_wait(release.tag, queue_path, on_wait)
                    response = self._request(f"/download/{token}", timeout=timeout)
                except MirrorError as exc:
                    if exc.status in (404, 409) and attempt == 0:
                        continue
                    raise
                with response:
                    if response.headers.get("Content-Length") != str(release.size):
                        raise MirrorError("VPS 安装包大小与索引不符")
                    on_start()
                    yield response
                return
            finally:
                try:
                    with self._request(queue_path, method="DELETE", timeout=2, attempts=1):
                        pass
                except MirrorError:
                    # The server expires tickets when a client cannot cancel explicitly.
                    pass
