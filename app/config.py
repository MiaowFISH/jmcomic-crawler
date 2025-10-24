from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Dict, Optional

import yaml
from pydantic import BaseModel, Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class ServerSettings(BaseSettings):
    # Server
    host: str = Field(default="0.0.0.0")
    port: int = Field(default=8000)

    # Paths
    data_dir: Path = Field(default_factory=lambda: Path(os.getenv("JM_SERVER_DATA", "./data")).resolve())
    cache_dir_name: str = Field(default="cache")
    work_dir_name: str = Field(default="work")
    tasks_state_file: str = Field(default="tasks_state.json")

    # Cache
    cache_ttl_hours: Optional[int] = Field(default=None)

    # default proxy may be overridden per-task
    default_proxy: Optional[str] = Field(default=None, description="Global default proxy, e.g., 127.0.0.1:7890 or 'system'. Can be overridden per task.")

    # Concurrency
    max_workers: int = Field(default=max(4, os.cpu_count() or 4))

    # Features
    enable_mcp: bool = Field(default=True)

    model_config = SettingsConfigDict(env_prefix="JM_SERVER_", env_nested_delimiter="__")

    @property
    def cache_dir(self) -> Path:
        return self.data_dir / self.cache_dir_name

    @property
    def work_dir(self) -> Path:
        return self.data_dir / self.work_dir_name

    @property
    def tasks_state_path(self) -> Path:
        return self.data_dir / self.tasks_state_file


class AppConfig(BaseModel):
    server: ServerSettings
    # Raw jmcomic option config loaded from config.yml under top-level key 'jmcomic'
    jmcomic: Optional[Dict[str, Any]] = None

    @classmethod
    def load(cls, repo_root: Optional[Path] = None) -> "AppConfig":
        # Load from config.yml if present and merge into defaults
        repo_root = repo_root or Path.cwd()
        settings = ServerSettings()
        jmcomic_cfg: Optional[Dict[str, Any]] = None

        cfg_file = repo_root / "config.yml"
        if cfg_file.exists():
            try:
                raw = yaml.safe_load(cfg_file.read_text(encoding="utf-8")) or {}
                server_cfg = raw.get("server") or {}
                jmcomic_cfg = raw.get("jm_comic")

                # Coerce known fields
                if "host" in server_cfg:
                    settings.host = server_cfg["host"]
                if "port" in server_cfg:
                    settings.port = int(server_cfg["port"])
                if "data_dir" in server_cfg:
                    settings.data_dir = Path(server_cfg["data_dir"]).resolve()
                if "cache_ttl_hours" in server_cfg:
                    settings.cache_ttl_hours = server_cfg["cache_ttl_hours"]
                if "default_proxy" in server_cfg:
                    settings.default_proxy = server_cfg["default_proxy"]
                if "max_workers" in server_cfg:
                    settings.max_workers = int(server_cfg["max_workers"])
                if "enable_mcp" in server_cfg:
                    settings.enable_mcp = bool(server_cfg["enable_mcp"])
            except Exception:
                # Do not crash on config parse errors; rely on defaults
                pass

        # Ensure dirs exist
        settings.data_dir.mkdir(parents=True, exist_ok=True)
        settings.cache_dir.mkdir(parents=True, exist_ok=True)
        settings.work_dir.mkdir(parents=True, exist_ok=True)

        return cls(server=settings, jmcomic=jmcomic_cfg)
