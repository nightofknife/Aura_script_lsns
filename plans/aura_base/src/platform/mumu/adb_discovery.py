# -*- coding: utf-8 -*-
from __future__ import annotations

import shlex
import subprocess
from dataclasses import dataclass
import re
from typing import List, Sequence

from ..contracts import TargetRuntimeError


@dataclass(frozen=True)
class AdbDeviceInfo:
    serial: str
    manufacturer: str = ""
    model: str = ""
    abi: str = ""


@dataclass(frozen=True)
class AdbDisplayInfo:
    physical_width: int = 0
    physical_height: int = 0
    current_orientation: int = 0


class AdbController:
    def __init__(self, executable: str = "adb", default_timeout_sec: float = 15.0):
        self.executable = str(executable or "adb")
        self.default_timeout_sec = max(float(default_timeout_sec or 15.0), 1.0)

    def run(
        self,
        args: Sequence[str],
        *,
        timeout_sec: float | None = None,
        check: bool = True,
    ) -> subprocess.CompletedProcess[str]:
        cmd = [self.executable, *[str(item) for item in args]]
        completed = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=timeout_sec or self.default_timeout_sec,
        )
        if check and completed.returncode != 0:
            raise TargetRuntimeError(
                "adb_command_failed",
                f"ADB command failed: {' '.join(cmd)}",
                {
                    "returncode": completed.returncode,
                    "stdout": completed.stdout,
                    "stderr": completed.stderr,
                },
            )
        return completed

    def connect(self, serial: str) -> str:
        completed = self.run(["connect", serial], check=False)
        output = (completed.stdout or completed.stderr or "").strip()
        if completed.returncode != 0:
            raise TargetRuntimeError(
                "adb_connect_failed",
                f"Failed to connect adb serial '{serial}'.",
                {"stdout": completed.stdout, "stderr": completed.stderr},
            )
        return output

    def list_devices(self) -> List[str]:
        completed = self.run(["devices"], check=True)
        devices: List[str] = []
        for raw_line in (completed.stdout or "").splitlines():
            line = raw_line.strip()
            if not line or line.startswith("List of devices"):
                continue
            parts = line.split()
            if len(parts) >= 2 and parts[1] == "device":
                devices.append(parts[0])
        return devices

    def shell(self, serial: str, args: Sequence[str], *, timeout_sec: float | None = None) -> str:
        completed = self.run(["-s", serial, "shell", *list(args)], timeout_sec=timeout_sec, check=True)
        return (completed.stdout or "").strip()

    def shell_script(self, serial: str, script: str, *, timeout_sec: float | None = None, check: bool = True) -> str:
        completed = self.run(
            ["-s", serial, "shell", f"sh -c {shell_quote(script)}"],
            timeout_sec=timeout_sec,
            check=check,
        )
        return (completed.stdout or completed.stderr or "").strip()

    def push(self, serial: str, local_path: str, remote_path: str):
        self.run(["-s", serial, "push", local_path, remote_path], check=True)

    def forward(self, serial: str, local_port: int, remote_port: int):
        self.run(
            ["-s", serial, "forward", f"tcp:{int(local_port)}", f"tcp:{int(remote_port)}"],
            check=True,
        )

    def forward_socket(self, serial: str, local_port: int, remote_spec: str):
        self.run(
            ["-s", serial, "forward", f"tcp:{int(local_port)}", str(remote_spec)],
            check=True,
        )

    def remove_forward(self, serial: str, local_port: int):
        self.run(
            ["-s", serial, "forward", "--remove", f"tcp:{int(local_port)}"],
            check=False,
        )

    def getprop(self, serial: str, key: str) -> str:
        return self.shell(serial, ["getprop", key])

    def get_device_info(self, serial: str) -> AdbDeviceInfo:
        return AdbDeviceInfo(
            serial=serial,
            manufacturer=self.getprop(serial, "ro.product.manufacturer"),
            model=self.getprop(serial, "ro.product.model"),
            abi=self.getprop(serial, "ro.product.cpu.abi"),
        )

    def get_display_info(self, serial: str) -> AdbDisplayInfo:
        size_output = self.run(["-s", serial, "shell", "wm", "size"], check=True)
        size_text = f"{size_output.stdout or ''}\n{size_output.stderr or ''}"
        match = re.search(r"Physical size:\s*(\d+)x(\d+)", size_text)
        if not match:
            raise TargetRuntimeError(
                "adb_display_size_unavailable",
                "Unable to parse physical display size from adb output.",
                {"serial": serial, "output": size_text.strip()},
            )

        dumpsys_output = self.run(["-s", serial, "shell", "dumpsys", "display"], check=False)
        dumpsys_text = f"{dumpsys_output.stdout or ''}\n{dumpsys_output.stderr or ''}"
        orientation_match = re.search(r"mCurrentOrientation=(\d+)", dumpsys_text)
        orientation = int(orientation_match.group(1)) if orientation_match else 0

        return AdbDisplayInfo(
            physical_width=int(match.group(1)),
            physical_height=int(match.group(2)),
            current_orientation=orientation % 4,
        )

    def input_keyevent(self, serial: str, keyevent: str):
        self.shell(serial, ["input", "keyevent", str(keyevent)])

    def input_text(self, serial: str, text: str):
        escaped = str(text).replace(" ", "%s")
        self.shell(serial, ["input", "text", escaped])


def shell_quote(value: str) -> str:
    return shlex.quote(str(value))
