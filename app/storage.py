from __future__ import annotations

import hashlib
import json
import os
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Optional

from .config import AppConfig


@dataclass
class CacheEntry:
    key: str
    path: Path
    created_at: float
    size: int


class Storage:
    def __init__(self, cfg: AppConfig) -> None:
        self.cfg = cfg
        self._lock = threading.Lock()
        self._state_path = self.cfg.server.tasks_state_path
        self._tasks: Dict[str, Dict[str, Any]] = {}
        self._load_state()

    # ---- task state persistence (best-effort) ----
    def _load_state(self) -> None:
        if self._state_path.exists():
            try:
                raw = json.loads(self._state_path.read_text(encoding="utf-8"))
                self._tasks = raw.get("tasks", {})
            except Exception:
                self._tasks = {}

    def _save_state(self) -> None:
        try:
            data = {"tasks": self._tasks}
            self._state_path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        except Exception:
            pass

    def get_task_state(self, task_id: str) -> Optional[Dict[str, Any]]:
        with self._lock:
            return self._tasks.get(task_id)

    def set_task_state(self, task_id: str, state: Dict[str, Any]) -> None:
        with self._lock:
            self._tasks[task_id] = state
        self._save_state()

    def iter_tasks(self) -> Dict[str, Dict[str, Any]]:
        with self._lock:
            return dict(self._tasks)

    # ---- cache helpers ----
    def cache_key(self, payload: Dict[str, Any]) -> str:
        # Compute a stable hash excluding sensitive plaintext password
        h = hashlib.sha256()
        safe = dict(payload)
        pwd = safe.pop("password", None)
        if pwd is not None:
            safe["password_hash"] = hashlib.sha256(pwd.encode("utf-8")).hexdigest()
        blob = json.dumps(safe, sort_keys=True, ensure_ascii=False).encode("utf-8")
        h.update(blob)
        return h.hexdigest()

    def cached_artifact(self, key: str) -> Optional[CacheEntry]:
        # The artifact path will be cache_dir/key.ext; we don't know ext, search
        cache_dir = self.cfg.server.cache_dir
        if not cache_dir.exists():
            return None
        for p in cache_dir.glob(f"{key}.*"):
            try:
                stat = p.stat()
                return CacheEntry(key=key, path=p, created_at=stat.st_mtime, size=stat.st_size)
            except FileNotFoundError:
                continue
        return None

    def put_artifact(self, key: str, src_path: Path, suffix: str) -> Path:
        cache_dir = self.cfg.server.cache_dir
        cache_dir.mkdir(parents=True, exist_ok=True)
        dst = cache_dir / f"{key}.{suffix.lstrip('.')}"
        if src_path.resolve() == dst.resolve():
            return dst
        # Copy to cache
        dst.write_bytes(src_path.read_bytes())
        # update mtime
        os.utime(dst, None)
        return dst
