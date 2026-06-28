# packages/aura_base/services/process_manager_service.py
import os
import platform
import subprocess
import threading
import time
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

import psutil

from packages.aura_core.api import service_info
from packages.aura_core.observability.logging.core_logger import logger
from packages.aura_core.context.persistence.store_service import StateStoreService
from .config_service import ConfigService

DETACHED_PROCESS = 0x00000008
CREATE_NEW_PROCESS_GROUP = 0x00000200
CREATE_BREAKAWAY_FROM_JOB = 0x01000000

_STATE_KEY = "process_manager.registry"


@dataclass
class ProcRecord:
    identifier: str
    pid: Optional[int]
    executable: str
    args: List[str]
    cwd: Optional[str]
    env: Optional[Dict[str, str]]
    started_at: float


@service_info(
    alias="process_manager",
    public=True,
    deps={"config": "core/config", "state_store": "core/state_store"},
)
class ProcessManagerService:
    """
    进程管理 Server（无 UI、纯能力）。
    - 负责配置解析、状态持久化、进程生命期管理
    - 保证父进程退出后子进程仍存活（Windows: DETACHED + BREAKAWAY；POSIX: start_new_session=True）
    """

    def __init__(self, config: ConfigService, state_store: StateStoreService):
        self._config = config
        self._state_store = state_store
        self._records: Dict[str, ProcRecord] = {}
        self._loaded = False

        # 同步/异步桥接（与 ScreenService 一致）
        import asyncio
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._loop_lock = threading.Lock()

    # =============== 公共同步 API（供 Action 调用） ===============

    def start_process(
        self,
        identifier: str,
        executable_path: Optional[str] = None,
        args: Optional[List[str]] = None,
        cwd: Optional[str] = None,
        env: Optional[Dict[str, str]] = None,
    ) -> Dict[str, Any]:
        if not identifier or not str(identifier).strip():
            return {"status": "error", "message": "identifier 不能为空。"}
        logger.info(f"[Process] 请求启动: identifier={identifier!r}, exe_in={executable_path!r}")

        self._ensure_loaded()

        # 若已记录并且仍在运行 -> 幂等返回
        rec = self._records.get(identifier)
        if rec and rec.pid and self._pid_alive(rec.pid, expect_exe=rec.executable):
            logger.info(f"[Process] '{identifier}' 已在运行 (pid={rec.pid})。")
            return {"status": "already_running", "pid": rec.pid}

        # 从配置解析（全部在 Server 内完成，Action 无状态）
        exe, final_args, final_cwd, final_env = self._resolve_from_config(
            identifier, executable_path, args, cwd, env
        )
        if not exe:
            return {"status": "error", "message": f"apps.{identifier}.executable/path 未配置且未传参。"}

        try:
            popen = self._spawn_detached(exe, final_args, final_cwd, final_env)
            popen = self._maybe_follow_successor(identifier, popen, expect_exe=exe)

        except Exception as e:
            logger.error(f"[Process] 启动失败: {e}", exc_info=True)
            return {"status": "error", "message": str(e)}

        pid = popen.pid
        self._records[identifier] = ProcRecord(
            identifier=identifier,
            pid=pid,
            executable=exe,
            args=final_args,
            cwd=final_cwd,
            env=final_env,
            started_at=time.time(),
        )
        self._persist()
        logger.info(f"[Process] '{identifier}' 启动成功 pid={pid}")
        return {"status": "success", "pid": pid}

    def stop_process(self, identifier: str, force: bool = False, timeout: float = 5.0) -> Dict[str, Any]:
        self._ensure_loaded()
        rec = self._records.get(identifier)
        if not rec or not rec.pid:
            return {"status": "not_found", "message": "未找到已记录的进程。"}

        proc = self._get_ps(rec.pid, expect_exe=rec.executable)
        if not proc:
            # 尝试基于窗口标题寻找仍在运行的真正进程
            title = self._config.get(f"apps.{identifier}.window_title") or self._config.get("app.target_window_title")
            exact = bool(self._config.get(f"apps.{identifier}.window_title_exact", False))
            if title and platform.system() == "Windows":
                pids = self._find_pids_by_window_title(title, exact=exact)
                if pids:
                    for pid in pids:
                        self._terminate_proc_tree(pid, force=force, timeout=timeout)
                    self._records.pop(identifier, None)
                    self._persist()
                    return {"status": "success", "message": f"已通过窗口标题({title})关闭 {len(pids)} 个进程。"}

            # 原逻辑：进程不存在则清理记录
            self._records.pop(identifier, None)
            self._persist()
            return {"status": "success", "message": "进程已不存在，记录已清理。"}

        try:
            if force:
                proc.kill()
            else:
                proc.terminate()
            proc.wait(timeout=timeout)
        except psutil.TimeoutExpired:
            if not force:
                return {"status": "timeout", "message": "优雅退出超时，可设置 force=True 再试。"}
            try:
                proc.kill()
                proc.wait(timeout=3)
            except Exception as e:
                return {"status": "error", "message": f"强杀失败: {e}"}
        except Exception as e:
            return {"status": "error", "message": str(e)}

        self._records.pop(identifier, None)
        self._persist()
        return {"status": "success"}

    def get_process_status(self, identifier: str) -> Dict[str, Any]:
        self._ensure_loaded()
        rec = self._records.get(identifier)
        if not rec or not rec.pid:
            return {"is_running": False, "pid": None, "info": None}

        proc = self._get_ps(rec.pid, expect_exe=rec.executable)
        if not proc:
            return {"is_running": False, "pid": None, "info": {"last_pid": rec.pid}}

        info = {
            "name": proc.name(),
            "exe": self._safe_proc_exe(proc),
            "cwd": rec.cwd,
            "cmdline": proc.cmdline(),
            "create_time": getattr(proc, "create_time", lambda: None)(),
        }
        return {"is_running": True, "pid": proc.pid, "info": info}

    def wait_for_exit(self, identifier: str, timeout: Optional[float] = None) -> Dict[str, Any]:
        self._ensure_loaded()
        rec = self._records.get(identifier)
        if not rec or not rec.pid:
            return {"status": "no_process"}

        proc = self._get_ps(rec.pid, expect_exe=rec.executable)
        if not proc:
            # 已退出
            self._records.pop(identifier, None)
            self._persist()
            return {"status": "exited"}

        try:
            proc.wait(timeout=timeout)
            self._records.pop(identifier, None)
            self._persist()
            return {"status": "exited"}
        except psutil.TimeoutExpired:
            return {"status": "timeout"}

    # =============== 内部实现 ===============

    def _resolve_from_config(
        self,
        identifier: str,
        exe_in: Optional[str],
        args_in: Optional[List[str]],
        cwd_in: Optional[str],
        env_in: Optional[Dict[str, str]],
    ):
        exe = exe_in or self._config.get(f"apps.{identifier}.executable") or self._config.get(
            f"apps.{identifier}.path"
        )
        args = (
            args_in
            if args_in is not None
            else (self._config.get(f"apps.{identifier}.args", []) or [])
        )
        if not isinstance(args, list):
            args = [str(args)]
        cwd = cwd_in if cwd_in is not None else self._config.get(f"apps.{identifier}.cwd")
        env_cfg = self._config.get(f"apps.{identifier}.env", None)
        env = env_in if env_in is not None else (env_cfg if isinstance(env_cfg, dict) else None)
        return exe, args, cwd, env

    def _spawn_detached(self, exe: str, args: List[str], cwd: Optional[str], env: Optional[Dict[str, str]]):
        kwargs: Dict[str, Any] = {
            "cwd": cwd or None,
            "env": (dict(os.environ) | env) if env else None,
            "close_fds": True,
        }

        if platform.system() == "Windows":
            creationflags = DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP
            # 尝试 BREAKAWAY，防止父进程 Job 约束
            try:
                creationflags |= CREATE_BREAKAWAY_FROM_JOB
            except Exception:
                pass
            kwargs["creationflags"] = creationflags
            return subprocess.Popen([exe, *args], **kwargs)
        else:
            # POSIX：独立会话，脱离父进程
            return subprocess.Popen([exe, *args], start_new_session=True, **kwargs)

    def _pid_alive(self, pid: int, expect_exe: Optional[str]) -> bool:
        proc = self._get_ps(pid, expect_exe=expect_exe)
        return proc is not None

    def _get_ps(self, pid: int, expect_exe: Optional[str]) -> Optional[psutil.Process]:
        try:
            proc = psutil.Process(pid)
            if not proc.is_running():
                return None
            if expect_exe:
                exe = self._safe_proc_exe(proc)
                # 路径大小写/符号差异容忍
                if exe and os.path.normcase(os.path.abspath(exe)) != os.path.normcase(os.path.abspath(expect_exe)):
                    # 可放宽到只比较文件名：
                    if os.path.basename(exe) != os.path.basename(expect_exe):
                        return None
            return proc
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            return None
        except Exception:
            return None

    def _safe_proc_exe(self, proc: psutil.Process) -> Optional[str]:
        try:
            return proc.exe()
        except Exception:
            return None

    # =============== 状态持久化（StateStore 是异步，这里桥接） ===============

    def _ensure_loaded(self):
        if self._loaded:
            return
        data = self._await(self._state_store.get(_STATE_KEY, default={}))
        if isinstance(data, dict):
            for k, v in data.items():
                try:
                    self._records[k] = ProcRecord(
                        identifier=k,
                        pid=v.get("pid"),
                        executable=v["executable"],
                        args=v.get("args", []),
                        cwd=v.get("cwd"),
                        env=v.get("env"),
                        started_at=v.get("started_at", 0.0),
                    )
                except Exception:
                    continue
        self._loaded = True

    def _persist(self):
        payload = {
            k: {
                "pid": v.pid,
                "executable": v.executable,
                "args": v.args,
                "cwd": v.cwd,
                "env": v.env,
                "started_at": v.started_at,
            }
            for k, v in self._records.items()
        }
        self._await(self._state_store.set(_STATE_KEY, payload))

    # ---- 与 ScreenService 相同风格的桥接 ----
    def _get_running_loop(self):
        with self._loop_lock:
            if self._loop is None or self._loop.is_closed():
                from packages.aura_core.api import service_registry
                scheduler = service_registry.get_service_instance('scheduler')
                if scheduler and scheduler._loop and scheduler._loop.is_running():
                    self._loop = scheduler._loop
                else:
                    raise RuntimeError("ProcessManagerService 无法找到正在运行的 asyncio 事件循环。")
            return self._loop

    def _await(self, coro):
        import asyncio
        loop = self._get_running_loop()
        try:
            running_loop = asyncio.get_running_loop()
        except RuntimeError:
            running_loop = None
        if running_loop is loop:
            raise RuntimeError("ProcessManagerService sync API called from event loop thread; use *_async to avoid deadlock.")
        fut = asyncio.run_coroutine_threadsafe(coro, loop)
        return fut.result()

    def _maybe_follow_successor(self, identifier, popen, expect_exe: Optional[str]):
        follow = bool(self._config.get(f"apps.{identifier}.follow_child", True))
        if not follow:
            return popen

        delay = float(self._config.get(f"apps.{identifier}.child_probe_delay", 1.0))
        title = self._config.get(f"apps.{identifier}.window_title") or self._config.get("app.target_window_title")
        exact = bool(self._config.get(f"apps.{identifier}.window_title_exact", False))
        time.sleep(delay)

        # 优先：通过窗口标题找到最终 PID
        if title and platform.system() == "Windows":
            pids = self._find_pids_by_window_title(title, exact=exact)
            if pids:
                class _Tmp:  # 伪 Popen，只要有 pid 属性即可
                    def __init__(self, pid): self.pid = pid

                return _Tmp(pids[0])

        # 其次：从后代进程里挑“最新创建”的一个
        try:
            parent = psutil.Process(popen.pid)
            descendants = parent.children(recursive=True)
            if descendants:
                cand = max((d for d in descendants if d.is_running()),
                           key=lambda d: getattr(d, "create_time", lambda: 0.0)() or 0.0)

                class _Tmp:
                    def __init__(self, pid): self.pid = pid

                return _Tmp(cand.pid)
        except Exception:
            pass
        return popen

    def _find_pids_by_window_title(self, title: str, exact: bool = False) -> List[int]:
        if platform.system() != "Windows":
            return []
        # 方案1：pywin32
        try:
            import win32gui, win32process
            pids = set()
            def cb(hwnd, _):
                if not win32gui.IsWindowVisible(hwnd):
                    return True
                text = win32gui.GetWindowText(hwnd) or ""
                ok = (text == title) if exact else (title.lower() in text.lower())
                if ok:
                    _, pid = win32process.GetWindowThreadProcessId(hwnd)
                    if pid:
                        pids.add(int(pid))
                return True
            win32gui.EnumWindows(cb, None)
            return list(pids)
        except Exception:
            pass

        # 方案2：ctypes（无 pywin32 也能跑）
        import ctypes
        from ctypes import wintypes
        user32 = ctypes.windll.user32
        GetWindowTextW = user32.GetWindowTextW
        GetWindowTextLengthW = user32.GetWindowTextLengthW
        IsWindowVisible = user32.IsWindowVisible
        EnumWindows = user32.EnumWindows
        GetWindowThreadProcessId = user32.GetWindowThreadProcessId

        EnumWindowsProc = ctypes.WINFUNCTYPE(ctypes.c_bool, wintypes.HWND, wintypes.LPARAM)
        pids = set()
        def py_cb(hwnd, lParam):
            if not IsWindowVisible(hwnd):
                return True
            length = GetWindowTextLengthW(hwnd)
            buf = ctypes.create_unicode_buffer(length + 1)
            GetWindowTextW(hwnd, buf, length + 1)
            text = buf.value or ""
            ok = (text == title) if exact else (title.lower() in text.lower())
            if ok:
                pid = wintypes.DWORD()
                GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
                if pid.value:
                    pids.add(int(pid.value))
            return True
        EnumWindows(EnumWindowsProc(py_cb), 0)
        return list(pids)

    def _terminate_proc_tree(self, pid: int, force: bool, timeout: float):
        try:
            root = psutil.Process(pid)
        except Exception:
            return
        # 先杀子进程，后杀自己
        for ch in root.children(recursive=True):
            try:
                (ch.kill() if force else ch.terminate())
            except Exception:
                pass
        try:
            (root.kill() if force else root.terminate())
            root.wait(timeout=timeout)
        except Exception:
            pass
