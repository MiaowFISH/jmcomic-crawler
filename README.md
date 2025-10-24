# JMComic Download Server

A FastAPI server that downloads JM comics by album ID, packages them into ZIP or PDF, supports encryption/password, caching, proxy, and MCP integration (fastmcp).

## Configure

Put your settings in `config.yml` at the repo root. Do NOT rely on JM_OPTION_PATH; all jmcomic settings can live here.

Example:

```yaml
server:
	host: 0.0.0.0
	port: 8000
	data_dir: ./data
	default_proxy: system  # or 127.0.0.1:7890

jmcomic:
	log: true
	client:
		impl: html
		retry_times: 5
		postman:
			meta_data:
				proxies: system
	download:
		cache: true
		image:
			decode: true
		threading:
			image: 16
			photo: 8
	dir_rule:
		# base_dir will be overridden per-task to the task workspace
		base_dir: ./data/work/
		rule: Bd / Aname / Ptitle
```

Notes:
- The server will override `dir_rule.base_dir` to a per-task workspace automatically.
- You can override proxy per task with the `proxy` field.

## Run the server

Use the script entry point:

```powershell
uv run start
```

Or directly with Uvicorn:

```powershell
uv run python -m uvicorn app.main:app --host 0.0.0.0 --port 8000
```

## REST API

- POST `/tasks` Submit a download job

Request body:

```json
{
	"album_ids": ["123", "456"],
	"output_format": "zip",   // or "pdf"
	"quality": 85,             // optional JPEG re-encode quality 1..100
	"encrypt": true,           // zip/pdf encryption
	"password": "secret",     // optional password
	"compression": 6,          // zip compression 0..9
	"cache": true,             // reuse existing artifact if available
	"proxy": "system"         // optional per-task proxy override
}
```

Response:

```json
{ "task_id": "...", "status": "queued", "duplicate": false }
```

- GET `/tasks/{task_id}` Get status. When complete, `download_url` holds the artifact URL.
- GET `/tasks` List recent tasks.
- GET `/tasks/{task_id}/download/{filename}` Download artifact.

## Caching and deduplication

- The server computes a cache key from the request payload (password hashed) and stores artifacts under `data/cache`.
- Submitting an identical request returns the same task or a completed result if cached.

## MCP (fastmcp)

Optional MCP server using `fastmcp` is included. Start it with:

```powershell
uv run python -c "from app.mcp_fast import start_mcp; start_mcp()"
```

Tools exposed:
- `submit_task(**kwargs like POST /tasks)` -> `{task_id, duplicate, status}`
- `task_status(task_id)` -> task status dict or null

## Custom downloader hook

The server subclasses `JmDownloader` to intercept `before_album` and collect album metadata (authors, tags, chapter count, etc.), which is included in task status metadata.

## Troubleshooting

- If image decoding or packaging fails, check logs in your console and ensure dependencies like Pillow, img2pdf, pikepdf are installed via `uv sync`.
- If access fails due to region, set a working proxy in `config.yml` or per task.

```powershell
# Sync dependencies on Windows PowerShell
uv sync
```

