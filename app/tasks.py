from __future__ import annotations

import asyncio
import json
import time
import uuid
from pathlib import Path
from typing import Any, Dict, Optional

from fastapi import BackgroundTasks

from .config import AppConfig
from .downloader import DownloadParams, Downloader
from .models import SubmitResponse, TaskRequest, TaskStatus, TaskStatusEnum
from .storage import Storage


class TaskManager:
    def __init__(self, cfg: AppConfig, storage: Storage) -> None:
        self.cfg = cfg
        self.storage = storage
        self.loop = asyncio.get_event_loop()
        self._tasks: Dict[str, asyncio.Task] = {}
        self._dedup: Dict[str, str] = {}  # cache_key -> task_id

    def _now(self) -> float:
        return time.time()

    def _task_state(self, task_id: str) -> Dict[str, Any]:
        state = self.storage.get_task_state(task_id)
        if not state:
            state = {
                "task_id": task_id,
                "status": TaskStatusEnum.queued.value,
                "progress": 0.0,
                "created_at": self._now(),
                "updated_at": self._now(),
                "metadata": {},
                "artifact_path": None,
                "download_url": None,
            }
            self.storage.set_task_state(task_id, state)
        return state

    def submit(self, req: TaskRequest) -> SubmitResponse:
        # Dedup by cache key of request params (excluding non-deterministic fields)
        cache_payload = {
            "album_ids": sorted([str(a) for a in req.album_ids]),
            "output_format": req.output_format.value,
            "quality": req.quality,
            "encrypt": req.encrypt,
            "password": req.password or "",
            "compression": req.compression,
            "proxy": req.proxy or "",
            "option_file": str(req.option_file or ""),
        }
        cache_key = self.storage.cache_key(cache_payload)

        # Cache hit as completed artifact
        entry = self.storage.cached_artifact(cache_key) if req.cache else None
        if entry is not None:
            # Create a synthetic completed task referencing cached file
            task_id = self._dedup.get(cache_key) or uuid.uuid4().hex
            self._dedup[cache_key] = task_id
            state = self._task_state(task_id)
            state.update(
                {
                    "status": TaskStatusEnum.completed.value,
                    "progress": 1.0,
                    "updated_at": self._now(),
                    "artifact_path": str(entry.path),
                }
            )
            self.storage.set_task_state(task_id, state)
            return SubmitResponse(task_id=task_id, status=TaskStatusEnum.completed, duplicate=True)

        # If a running/queued task exists for the same key, return it
        if cache_key in self._dedup:
            task_id = self._dedup[cache_key]
            st = self._task_state(task_id)
            return SubmitResponse(task_id=task_id, status=TaskStatusEnum(st["status"]), duplicate=True)

        # Create a new task
        task_id = uuid.uuid4().hex
        self._dedup[cache_key] = task_id
        state = self._task_state(task_id)
        state["request"] = json.loads(req.model_dump_json())
        state["cache_key"] = cache_key
        self.storage.set_task_state(task_id, state)

        # Schedule background execution
        asyncio.create_task(self._run_task(task_id, req))
        return SubmitResponse(task_id=task_id, status=TaskStatusEnum.queued, duplicate=False)

    async def _run_task(self, task_id: str, req: TaskRequest) -> None:
        state = self._task_state(task_id)
        state.update({"status": TaskStatusEnum.running.value, "updated_at": self._now()})
        self.storage.set_task_state(task_id, state)

        # Prepare work dir and downloader
        work_dir = self.cfg.server.work_dir / task_id
        dl = Downloader(work_dir, self.cfg)

        def progress_cb(p: float, msg: str) -> None:
            s = self._task_state(task_id)
            s["progress"] = max(0.0, min(0.99, float(p)))
            s["message"] = msg
            s["updated_at"] = self._now()
            self.storage.set_task_state(task_id, s)

        try:
            params = DownloadParams(
                album_ids=[str(a) for a in req.album_ids],
                output_format=req.output_format.value,
                quality=req.quality,
                encrypt=req.encrypt,
                password=req.password,
                compression=req.compression or 6,
                proxy=req.proxy if req.proxy is not None else self.cfg.server.default_proxy,
                option_file=Path(req.option_file).resolve() if req.option_file else None,
            )
            result = await asyncio.to_thread(dl.download_and_package, params, progress_cb)

            # Move artifact to cache path
            key = state["cache_key"]
            cached_path = self.storage.put_artifact(key, result.artifact_path, result.suffix)
            state.update(
                {
                    "status": TaskStatusEnum.completed.value,
                    "progress": 1.0,
                    "updated_at": self._now(),
                    "artifact_path": str(cached_path),
                    "metadata": result.metadata,
                }
            )
            self.storage.set_task_state(task_id, state)
        except Exception as e:
            state.update(
                {
                    "status": TaskStatusEnum.failed.value,
                    "error": str(e),
                    "updated_at": self._now(),
                }
            )
            self.storage.set_task_state(task_id, state)
            return

    def get_status(self, task_id: str) -> Optional[TaskStatus]:
        st = self.storage.get_task_state(task_id)
        if not st:
            return None
        # derive download_url lazily from artifact_path if available
        artifact = st.get("artifact_path")
        if artifact and not st.get("download_url"):
            filename = Path(artifact).name
            st["download_url"] = f"/tasks/{task_id}/download/{filename}"
            self.storage.set_task_state(task_id, st)
        return TaskStatus(**st)

    def list_tasks(self) -> Dict[str, Any]:
        return self.storage.iter_tasks()
