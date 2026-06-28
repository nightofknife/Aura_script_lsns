# -*- coding: utf-8 -*-
"""框架更新工具：应用本地或远程 zip 更新包，支持备份与保留目录/文件。"""
from __future__ import annotations

import shutil
import tempfile
import zipfile
try:
    import requests
    REQUESTS_AVAILABLE = True
except ImportError:
    REQUESTS_AVAILABLE = False
    requests = None  # type: ignore

from pathlib import Path
from typing import Dict, List, Optional

from packages.aura_core.config.loader import get_config_section
from packages.aura_core.observability.logging.core_logger import logger


class FrameworkUpdater:
    """应用本地更新包并备份当前版本。"""

    def __init__(self, base_path: Path):
        self.base_path = Path(base_path).resolve()
        cfg = get_config_section("framework_update", {}) or {}
        self.download_dir = Path(cfg.get("download_dir", "updates")).resolve()
        self.backup_dir = Path(cfg.get("backup_dir", "backups/framework")).resolve()
        self.source_url = cfg.get("source_url") or ""
        self.preserve = set(cfg.get("preserve", [])) | {
            "config.yaml",
            "logs",
            "backups",
            "updates",
        }

    def _latest_zip(self) -> Optional[Path]:
        if not self.download_dir.is_dir():
            return None
        zips = [p for p in self.download_dir.glob("*.zip") if p.is_file()]
        if not zips:
            return None
        return max(zips, key=lambda p: p.stat().st_mtime)

    def _download_remote(self) -> Optional[Path]:
        """从配置的 source_url 下载 zip 到 download_dir。"""
        if not self.source_url:
            return None
        try:
            self.download_dir.mkdir(parents=True, exist_ok=True)
            filename = self.source_url.rstrip("/").split("/")[-1] or "update.zip"
            dest = self.download_dir / filename
            logger.info("Downloading update from %s -> %s", self.source_url, dest)
            with requests.get(self.source_url, stream=True, timeout=120) as r:
                r.raise_for_status()
                with open(dest, "wb") as f:
                    for chunk in r.iter_content(chunk_size=8192):
                        if chunk:
                            f.write(chunk)
            return dest
        except Exception as exc:
            logger.error("Download failed from %s: %s", self.source_url, exc)
            return None

    def _resolve_root(self, extract_dir: Path) -> Path:
        """尝试找到解压后的根目录（若 zip 内有单一顶层目录则返回它）。"""
        children = [p for p in extract_dir.iterdir()]
        if len(children) == 1 and children[0].is_dir():
            return children[0]
        return extract_dir

    def _copytree(self, src: Path, dst: Path):
        """递归复制，支持覆盖。"""
        if src.is_dir():
            dst.mkdir(parents=True, exist_ok=True)
            for item in src.iterdir():
                self._copytree(item, dst / item.name)
        else:
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dst)

    def _filter_preserve(self, entries: List[Path]) -> List[Path]:
        return [p for p in entries if p.name not in self.preserve]

    def apply(self, zip_path: Optional[str] = None, backup: bool = True) -> Dict[str, str]:
        """应用更新包。zip_path 为空时优先用 updates 中最新 zip，若不存在则尝试远程下载。"""
        zip_file = Path(zip_path).resolve() if zip_path else self._latest_zip()
        if (not zip_path) and (zip_file is None):
            zip_file = self._download_remote()
        if not zip_file or not zip_file.is_file():
            return {"status": "error", "message": "No valid zip found to apply."}

        logger.info("Applying framework update from %s", zip_file)
        with tempfile.TemporaryDirectory() as tmpdir:
            extract_dir = Path(tmpdir) / "extract"
            extract_dir.mkdir(parents=True, exist_ok=True)
            try:
                with zipfile.ZipFile(zip_file, "r") as zf:
                    zf.extractall(extract_dir)
            except Exception as exc:
                logger.error("Failed to extract update zip: %s", exc)
                return {"status": "error", "message": f"Extract failed: {exc}"}

            root = self._resolve_root(extract_dir)

            backup_path = None
            if backup:
                backup_path = self.backup_dir / f"{zip_file.stem}"
                backup_path.mkdir(parents=True, exist_ok=True)
                entries = self._filter_preserve([p for p in self.base_path.iterdir()])
                for entry in entries:
                    target = backup_path / entry.name
                    self._copytree(entry, target)
                logger.info("Backup created at %s", backup_path)

            # 替换文件：先删除现有非保留项，再复制新内容
            for entry in self._filter_preserve([p for p in self.base_path.iterdir()]):
                if entry.exists():
                    shutil.rmtree(entry, ignore_errors=True) if entry.is_dir() else entry.unlink(missing_ok=True)

            for entry in self._filter_preserve([p for p in root.iterdir()]):
                dest = self.base_path / entry.name
                self._copytree(entry, dest)

        return {
            "status": "success",
            "message": f"Updated from {zip_file.name}",
            "zip": str(zip_file),
            "backup": str(backup_path) if backup else None,
        }
