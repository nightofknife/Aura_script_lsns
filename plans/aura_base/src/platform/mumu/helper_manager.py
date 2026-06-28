# -*- coding: utf-8 -*-
from __future__ import annotations

import json
import subprocess
import shutil
import time
import urllib.request
from pathlib import Path
from typing import Any, Dict, List

from ..contracts import TargetRuntimeError
from .adb_discovery import AdbController, shell_quote
from .runtime_assets import resolve_android_touch_helper_path


class AndroidTouchHelperManager:
    def __init__(
        self,
        adb: AdbController,
        serial: str,
        config: Dict[str, Any],
    ):
        self.adb = adb
        self.serial = serial
        self.config = dict(config or {})
        self.remote_dir = str(self.config.get("remote_dir") or "/data/local/tmp/aura")
        self.remote_name = str(self.config.get("remote_name") or "touch")
        self.remote_port = int(self.config.get("server_port") or 9889)
        self.local_port = int(self.config.get("local_port") or self.remote_port)
        self.auto_push = bool(self.config.get("auto_push", True))
        self.auto_start = bool(self.config.get("auto_start", True))
        self.healthcheck_interval_ms = int(self.config.get("healthcheck_interval_ms") or 1000)
        self.start_args = [str(item) for item in (self.config.get("start_args") or [])]
        self.endpoint = str(self.config.get("endpoint") or "/")
        self.log_path = str(self.config.get("log_path") or f"{self.remote_dir.rstrip('/')}/android_touch.log")
        self._last_healthcheck_at = 0.0
        self._healthy = False

    @property
    def remote_path(self) -> str:
        return f"{self.remote_dir.rstrip('/')}/{self.remote_name}"

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.local_port}{self.endpoint}"

    def ensure_ready(self):
        now_ms = time.monotonic() * 1000.0
        if self._healthy and now_ms - self._last_healthcheck_at < self.healthcheck_interval_ms:
            return

        self._ensure_forward()
        if not self.is_healthy():
            if self.auto_push:
                self._push_helper()
            if self.auto_start:
                self._start_helper()
            self._wait_for_health()
        self._healthy = True
        self._last_healthcheck_at = time.monotonic() * 1000.0

    def is_healthy(self) -> bool:
        try:
            self._post_json([{"type": "commit"}], timeout_sec=1.0)
        except Exception:
            self._healthy = False
            return False
        self._healthy = True
        self._last_healthcheck_at = time.monotonic() * 1000.0
        return True

    def send_commands(self, commands: List[Dict[str, Any]]) -> Any:
        self.ensure_ready()
        try:
            raw = self._post_json(list(commands), timeout_sec=3.0)
        except Exception as exc:
            self._healthy = False
            raise TargetRuntimeError(
                "input_helper_request_failed",
                "android_touch helper request failed.",
                {"serial": self.serial, "error": str(exc)},
            ) from exc

        if not raw:
            return None
        try:
            return json.loads(raw.decode("utf-8"))
        except Exception:
            return raw.decode("utf-8", errors="ignore")

    def close(self):
        self.adb.remove_forward(self.serial, self.local_port)
        self._healthy = False

    def _resolve_helper_path(self) -> Path:
        helper_path = str(self.config.get("helper_path") or "android_touch")
        direct = Path(helper_path)
        if direct.is_file():
            return direct.resolve()
        discovered = shutil.which(helper_path)
        if discovered:
            return Path(discovered).resolve()
        if helper_path in {"android_touch", "auto", "builtin"}:
            device_abi = self.adb.get_device_info(self.serial).abi
            builtin_path = resolve_android_touch_helper_path(device_abi)
            if builtin_path.is_file():
                return builtin_path.resolve()
            raise TargetRuntimeError(
                "input_helper_missing",
                "android_touch helper asset is missing for the current device ABI.",
                {
                    "helper_path": helper_path,
                    "serial": self.serial,
                    "abi": device_abi,
                    "expected_path": str(builtin_path),
                },
            )
        raise TargetRuntimeError(
            "input_helper_missing",
            f"android_touch helper not found: {helper_path}",
            {"helper_path": helper_path},
        )

    def _ensure_forward(self):
        self.adb.remove_forward(self.serial, self.local_port)
        self.adb.forward(self.serial, self.local_port, self.remote_port)

    def _push_helper(self):
        helper_path = self._resolve_helper_path()
        self.adb.shell_script(
            self.serial,
            f"mkdir -p {shell_quote(self.remote_dir)}",
            timeout_sec=5.0,
        )
        self.adb.push(self.serial, str(helper_path), self.remote_path)
        self.adb.shell_script(
            self.serial,
            f"chmod 755 {shell_quote(self.remote_path)}",
            timeout_sec=5.0,
        )

    def _start_helper(self):
        args = list(self.start_args)
        if not args and self.remote_port != 9889:
            args = [str(self.remote_port)]
        command = " ".join([shell_quote(self.remote_path), *[shell_quote(item) for item in args]])
        try:
            self.adb.shell_script(
                self.serial,
                f"mkdir -p {shell_quote(self.remote_dir)} && {command} >{shell_quote(self.log_path)} 2>&1 </dev/null &",
                timeout_sec=5.0,
                check=False,
            )
        except subprocess.TimeoutExpired:
            # Some adb + Android shell combinations do not detach the background
            # process cleanly, but the helper still starts. Continue into the
            # explicit health-wait path to confirm readiness.
            return

    def _wait_for_health(self):
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline:
            if self.is_healthy():
                return
            time.sleep(0.1)
        raise TargetRuntimeError(
            "input_helper_unavailable",
            "android_touch helper did not become healthy in time.",
            {"serial": self.serial, "local_port": self.local_port},
        )

    def _post_json(self, payload: List[Dict[str, Any]], *, timeout_sec: float) -> bytes:
        request = urllib.request.Request(
            self.base_url,
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=timeout_sec) as response:
            return response.read()
