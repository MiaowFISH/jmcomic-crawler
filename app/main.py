from __future__ import annotations

import asyncio
import mimetypes
from pathlib import Path
from typing import Optional

import uvicorn
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, JSONResponse

from .config import AppConfig
from .models import ListTasksResponse, SubmitResponse, TaskRequest, TaskStatus
from .storage import Storage
from .tasks import TaskManager

app = FastAPI(title="JMComic Download Server", version="0.1.0")

_cfg = AppConfig.load(Path(__file__).resolve().parents[1])
_storage = Storage(_cfg)
_manager = TaskManager(_cfg, _storage)


@app.get("/health")
async def health() -> dict:
    return {"status": "ok"}


@app.post("/tasks", response_model=SubmitResponse)
async def submit_task(req: TaskRequest) -> SubmitResponse:
    return _manager.submit(req)


@app.get("/tasks/{task_id}", response_model=TaskStatus)
async def get_task(task_id: str) -> TaskStatus:
    st = _manager.get_status(task_id)
    if not st:
        raise HTTPException(status_code=404, detail="Task not found")
    return st


@app.get("/tasks", response_model=ListTasksResponse)
async def list_tasks() -> ListTasksResponse:
    items = [TaskStatus(**v) for v in _manager.list_tasks().values()]
    return ListTasksResponse(
        tasks=sorted(items, key=lambda x: x.created_at, reverse=True)
    )


@app.get("/tasks/{task_id}/download/{filename}")
async def download_artifact(task_id: str, filename: str):
    st = _manager.get_status(task_id)
    if not st or not st.artifact_path:
        raise HTTPException(status_code=404, detail="Artifact not found")
    path = Path(st.artifact_path)
    if not path.exists() or path.name != filename:
        raise HTTPException(status_code=404, detail="File not found")
    media_type, _ = mimetypes.guess_type(str(path))
    return FileResponse(
        str(path),
        media_type=media_type or "application/octet-stream",
        filename=path.name,
    )


def start() -> None:  # entry point defined in pyproject
    uvicorn.run(
        "app.main:app",
        host=_cfg.server.host,
        port=_cfg.server.port,
        reload=False,
        workers=1,
    )
