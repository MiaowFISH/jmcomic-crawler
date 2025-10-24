from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, Optional

from .config import AppConfig
from .models import TaskRequest
from .storage import Storage
from .tasks import TaskManager


def start_mcp() -> None:
    """Start an MCP server using fastmcp. Optional extra entrypoint.

    Tools:
    - submit_task(args: TaskRequest fields)
    - task_status(task_id: str)
    """
    try:
        import fastmcp  # type: ignore
    except Exception as e:  # pragma: no cover
        raise RuntimeError("fastmcp is not installed. Install with 'pip install fastmcp'.") from e

    cfg = AppConfig.load(Path(__file__).resolve().parents[1])
    storage = Storage(cfg)
    manager = TaskManager(cfg, storage)

    mcp = fastmcp.FastMCP("jmcomic-download-server")

    @mcp.tool()
    def submit_task(**kwargs) -> Dict[str, Any]:  # type: ignore
        tr = TaskRequest(**kwargs)
        res = manager.submit(tr)
        return {"task_id": res.task_id, "duplicate": res.duplicate, "status": res.status.value}

    @mcp.tool()
    def task_status(task_id: str) -> Optional[Dict[str, Any]]:  # type: ignore
        st = manager.get_status(task_id)
        return json.loads(st.model_dump_json()) if st else None

    mcp.run()
