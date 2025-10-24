from __future__ import annotations

from enum import Enum
from pathlib import Path
from typing import Any, Dict, List, Literal, Optional, Union

from pydantic import BaseModel, Field, field_validator


class OutputFormat(str, Enum):
    zip = "zip"
    pdf = "pdf"


class TaskRequest(BaseModel):
    album_ids: List[Union[int, str]] = Field(..., description="Album IDs to download")
    output_format: OutputFormat = Field(default=OutputFormat.zip)
    # Image processing quality (0-100). If None, keep originals.
    quality: Optional[int] = Field(default=None, ge=1, le=100)
    # Zip/PDF encryption
    encrypt: bool = Field(default=False)
    password: Optional[str] = Field(default=None)
    # Zip compression level (0-9) when output_format=zip
    compression: Optional[int] = Field(default=6, ge=0, le=9)
    # Use cache if already available
    cache: bool = Field(default=True)
    # Optional proxy override (e.g., 'system', '127.0.0.1:7890')
    proxy: Optional[str] = Field(default=None)
    # Optional path to jmcomic option file for this task
    option_file: Optional[str] = Field(default=None)

    @field_validator("album_ids", mode="before")
    @classmethod
    def _coerce_ids(cls, v: Any) -> List[Union[int, str]]:
        if isinstance(v, (int, str)):
            return [v]
        return list(v or [])


class TaskStatusEnum(str, Enum):
    queued = "queued"
    running = "running"
    completed = "completed"
    failed = "failed"
    canceled = "canceled"


class TaskStatus(BaseModel):
    task_id: str
    status: TaskStatusEnum
    progress: float = 0.0
    message: Optional[str] = None
    error: Optional[str] = None
    created_at: float
    updated_at: float
    # Album metadata collected before download
    metadata: Dict[str, Any] = Field(default_factory=dict)
    # Where the artifact is stored if completed
    artifact_path: Optional[str] = None
    download_url: Optional[str] = None
    # Request snapshot to compute cache key or debug
    request: TaskRequest


class SubmitResponse(BaseModel):
    task_id: str
    status: TaskStatusEnum
    duplicate: bool = False


class ListTasksResponse(BaseModel):
    tasks: List[TaskStatus]
