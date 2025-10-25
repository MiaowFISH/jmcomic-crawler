import os
import json
import threading
import hashlib
import uuid
import time
from pathlib import Path
from typing import Optional, Dict, Any, List

import yaml
from fastapi import FastAPI, HTTPException
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from jmcomic import create_option_by_str, download_album, JmModuleConfig, JmDownloader, JmOption
from jmcomic.jm_option import JmAlbumDetail, JmPhotoDetail, JmImageDetail

REPO_ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = REPO_ROOT / "config.yml"

def load_config() -> Dict[str, Any]:
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}

CONFIG: Dict[str, Any] = load_config()

SERVER_CFG: Dict[str, Any] = CONFIG.get("server", {})
JM_CFG: Dict[str, Any] = CONFIG.get("jm_comic", {})

DATA_DIR = Path(SERVER_CFG.get("data_dir", "./data")).resolve()
ARTIFACTS_DIR = DATA_DIR / "artifacts"
WORK_DIR = DATA_DIR / "work"

ARTIFACTS_DIR.mkdir(parents=True, exist_ok=True)
WORK_DIR.mkdir(parents=True, exist_ok=True)

class TaskRequest(BaseModel):
    album_id: str = Field(..., description="JM album id")
    output_format: str = Field("zip", pattern="^(zip|pdf)$")
    quality: Optional[int] = Field(None, ge=1, le=100, description="JPEG re-encode quality")
    encrypt: bool = Field(False)
    password: Optional[str] = Field(None, description="Password if encrypt is true; if absent and encrypt=true, generate random")
    compression: int = Field(6, ge=0, le=9, description="Zip compression level")
    proxy: Optional[str] = Field(None, description="HTTP proxy host:port")

class TaskStatus(BaseModel):
    task_id: str
    album_id: str
    status: str
    progress: int = 0
    total_images: Optional[int] = None
    stage: Optional[str] = None
    duplicate: bool = False
    metadata: Dict[str, Any] = {}
    download_url: Optional[str] = None
    artifact_filename: Optional[str] = None
    error: Optional[str] = None
    password: Optional[str] = None

CURRENT_TASK_MANAGER: Optional[Any] = None

def set_current_task_manager(tm: Any):
    global CURRENT_TASK_MANAGER
    CURRENT_TASK_MANAGER = tm

class AppDownloader(JmDownloader):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.task_manager: Optional[Any] = CURRENT_TASK_MANAGER

    def before_album(self, album: JmAlbumDetail):
        try:
            if self.task_manager:
                self.task_manager.on_before_album(album)
        except Exception:
            pass
        return super().before_album(album)

    def after_album(self, album: JmAlbumDetail):
        try:
            if self.task_manager:
                self.task_manager.on_after_album(album)
        except Exception:
            pass
        return super().after_album(album)

    def before_photo(self, photo: JmPhotoDetail):
        try:
            if self.task_manager:
                self.task_manager.on_before_photo(photo)
        except Exception:
            pass
        return super().before_photo(photo)

    def after_photo(self, photo: JmPhotoDetail):
        try:
            if self.task_manager:
                self.task_manager.on_after_photo(photo)
        except Exception:
            pass
        return super().after_photo(photo)

    def before_image(self, image: JmImageDetail, img_save_path):
        try:
            if self.task_manager:
                self.task_manager.on_before_image(image, img_save_path)
        except Exception:
            pass
        return super().before_image(image, img_save_path)

    def after_image(self, image: JmImageDetail, img_save_path):
        try:
            if self.task_manager:
                self.task_manager.on_after_image(image, img_save_path)
        except Exception:
            pass
        return super().after_image(image, img_save_path)

JmModuleConfig.CLASS_DOWNLOADER = AppDownloader # type: ignore

class TaskManager:
    def __init__(self, artifacts_dir: Path, work_dir: Path, static_route: str):
        self.artifacts_dir = artifacts_dir
        self.work_dir = work_dir
        self.static_route = static_route
        self.tasks: Dict[str, TaskStatus] = {}
        self.album_state: Dict[str, Dict[str, Any]] = {}
        self.artifact_cache: Dict[str, Dict[str, str]] = {}
        self._lock = threading.RLock()
        # artifact dir naming map (album_id -> dir_name)
        self.artifact_map: Dict[str, str] = {}
        self._artifact_map_path: Path = DATA_DIR / "artifact_map.json"
        self._load_artifact_map()

    def _load_artifact_map(self):
        try:
            if self._artifact_map_path.exists():
                self.artifact_map = json.loads(self._artifact_map_path.read_text(encoding="utf-8"))
            else:
                self.artifact_map = {}
        except Exception:
            self.artifact_map = {}

    def _save_artifact_map(self):
        try:
            self._artifact_map_path.parent.mkdir(parents=True, exist_ok=True)
            self._artifact_map_path.write_text(json.dumps(self.artifact_map, ensure_ascii=False, indent=2), encoding="utf-8")
        except Exception:
            pass

    def _compute_dir_name(self, album_id: str) -> str:
        cfg = SERVER_CFG.get("artifact_name", {}) or {}
        rule = cfg.get("rule", "album_id")
        if rule == "album_id":
            return str(album_id)
        if rule == "short_hash":
            length = int(cfg.get("short_hash", {}).get("length", 8))
            return hashlib.sha1(str(album_id).encode("utf-8")).hexdigest()[:length]
        if rule == "date":
            fmt = cfg.get("date", {}).get("format", "%Y%m%d")
            from datetime import datetime
            return datetime.now().strftime(fmt)
        if rule == "random":
            sub = cfg.get("random", {})
            length = int(sub.get("length", 8))
            charset = sub.get("charset", "ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz23456789")
            import secrets
            return "".join(secrets.choice(charset) for _ in range(length))
        return str(album_id)

    def _artifact_dir_for(self, album_id: str) -> Path:
        dir_name = self.artifact_map.get(str(album_id))
        if not dir_name:
            dir_name = self._compute_dir_name(album_id)
            self.artifact_map[str(album_id)] = dir_name
            self._save_artifact_map()
        p = self.artifacts_dir / dir_name
        p.mkdir(parents=True, exist_ok=True)
        return p

    def submit(self, req: TaskRequest) -> TaskStatus:
        with self._lock:
            task_id = uuid.uuid4().hex
            status = TaskStatus(
                task_id=task_id,
                album_id=req.album_id,
                status="queued",
                progress=0,
                duplicate=False,
                stage="queued",
            )
            self.tasks[task_id] = status

            cache_hash = self._hash_params(req)
            status.metadata["cache_hash"] = cache_hash
            status.metadata["output_format"] = req.output_format
            status.metadata["quality"] = req.quality
            status.metadata["encrypt"] = req.encrypt
            status.metadata["compression"] = req.compression
            status.metadata["proxy"] = req.proxy

            album_ws = self._ensure_workspace(req.album_id)

            # if artifact for these parameters already exists, return immediately
            existing = self._get_artifact(req.album_id, cache_hash, req.output_format)
            if existing:
                filename = existing
                url = f"{self.static_route}/{req.album_id}/{filename}"
                status.status = "done"
                status.stage = "done"
                status.artifact_filename = filename
                status.download_url = url
                status.duplicate = True
                return status

            # if album already fully downloaded previously, skip API and go straight to packaging
            complete, meta = self._is_album_complete(req.album_id)
            if complete:
                st = self.album_state.setdefault(
                    req.album_id,
                    {"status": "done", "workspace": album_ws, "metadata": {}, "progress": 0, "total_images": None, "error": None}
                )
                st["status"] = "done"
                st["workspace"] = album_ws
                st["metadata"] = meta or {}
                st["total_images"] = len(meta.get("images", [])) if isinstance(meta, dict) else None
                self.album_state[req.album_id] = st
                threading.Thread(target=self._package_task, args=(status.task_id, req, cache_hash), daemon=True).start()
                status.status = "packaging"
                status.stage = "packaging"
                status.duplicate = True
                return status

            st = self.album_state.setdefault(req.album_id, {"status": "idle", "workspace": album_ws, "metadata": {}, "progress": 0, "total_images": None, "error": None})

            if st["status"] in ("downloading", "processing"):
                status.duplicate = True
                status.status = "processing"
                status.stage = "processing"
                threading.Thread(target=self._await_and_package, args=(status.task_id, req, cache_hash), daemon=True).start()
                return status

            if st["status"] == "done":
                threading.Thread(target=self._package_task, args=(status.task_id, req, cache_hash), daemon=True).start()
                status.status = "packaging"
                status.stage = "packaging"
                return status

            st["status"] = "downloading"
            self.album_state[req.album_id] = st

            threading.Thread(target=self._download_then_package, args=(status.task_id, req, cache_hash), daemon=True).start()
            status.status = "downloading"
            status.stage = "downloading"
            return status

    def get(self, task_id: str) -> Optional[TaskStatus]:
        with self._lock:
            return self.tasks.get(task_id)

    def list_tasks(self) -> List[TaskStatus]:
        with self._lock:
            return list(self.tasks.values())

    def on_before_album(self, album: JmAlbumDetail):
        with self._lock:
            album_id = str(album.id)
            st = self.album_state.get(album_id)
            if st:
                meta = st["metadata"]
                try:
                    meta["album_id"] = album_id
                    title_or_name = getattr(album, "name", None) or getattr(album, "title", None)
                    meta["title"] = title_or_name
                    meta["name"] = title_or_name
                    meta["author"] = getattr(album, "author", None)
                    meta["tags"] = getattr(album, "tags", None)
                    # prefer page_count if available; else will compute after download
                    pc = getattr(album, "page_count", None)
                    if isinstance(pc, int):
                        meta["page_count"] = pc
                    meta.setdefault("images", [])
                except Exception:
                    pass

    def on_after_album(self, album: JmAlbumDetail):
        with self._lock:
            album_id = str(album.id)
            st = self.album_state.get(album_id)
            if st:
                st["status"] = "done"
                ws = st["workspace"]
                meta = st.get("metadata", {}) or {}
                # compute counts and mark complete
                images_list = meta.get("images")
                if not isinstance(images_list, list):
                    images_list = []
                # fallback: count files on disk
                try:
                    total_files = len(self._collect_images(album_id))
                except Exception:
                    total_files = len(images_list)
                meta["page_count"] = meta.get("page_count") or total_files or len(images_list)
                meta["total_images"] = total_files
                st["total_images"] = total_files
                meta["complete"] = True
                st["metadata"] = meta
                (ws / "meta.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")

    def on_before_photo(self, photo: JmPhotoDetail):
        with self._lock:
            album_id = str(photo.album_id)
            st = self.album_state.get(album_id)
            if st:
                st["stage"] = "downloading.photo"

    def on_after_photo(self, photo: JmPhotoDetail):
        with self._lock:
            album_id = str(photo.album_id)
            st = self.album_state.get(album_id)
            if st:
                st["stage"] = "downloading.photo.done"

    def on_before_image(self, image: JmImageDetail, img_save_path: str):
        with self._lock:
            album_id = str(image.aid)
            st = self.album_state.get(album_id)
            if st:
                st["progress"] = st.get("progress", 0) + 1
                meta = st.setdefault("metadata", {})
                images_list = meta.setdefault("images", [])
                try:
                    url = getattr(image, "img_url", None) or getattr(image, "url", None)
                    if url:
                        images_list.append(url)
                except Exception:
                    pass

    def on_after_image(self, image: JmImageDetail, img_save_path: str):
        pass

    def _download_then_package(self, task_id: str, req: TaskRequest, cache_hash: str):
        try:
            # short-circuit if album already complete on disk
            complete, meta = self._is_album_complete(req.album_id)
            if complete:
                with self._lock:
                    st = self.album_state.setdefault(req.album_id, {"status": "done", "workspace": self._ensure_workspace(req.album_id), "metadata": {}, "progress": 0, "total_images": None, "error": None})
                    st["status"] = "done"
                    st["metadata"] = meta or {}
                    st["total_images"] = len(meta.get("images", [])) if isinstance(meta, dict) else None
                    ts = self.tasks.get(task_id)
                    if ts:
                        ts.status = "processing"
                        ts.stage = "processing"
                self._package_task(task_id, req, cache_hash)
                return

            self._download_album(req)
            with self._lock:
                ts = self.tasks.get(task_id)
                if ts:
                    ts.status = "processing"
                    ts.stage = "processing"
            self._package_task(task_id, req, cache_hash)
        except Exception as e:
            with self._lock:
                st = self.album_state.get(req.album_id)
                if st:
                    st["status"] = "failed"
                    st["error"] = str(e)
                ts = self.tasks.get(task_id)
                if ts:
                    ts.status = "failed"
                    ts.error = str(e)

    def _await_and_package(self, task_id: str, req: TaskRequest, cache_hash: str):
        while True:
            with self._lock:
                st = self.album_state.get(req.album_id)
                done = st and st["status"] == "done"
                failed = st and st["status"] == "failed"
            if failed:
                with self._lock:
                    ts = self.tasks.get(task_id)
                    if ts:
                        ts.status = "failed"
                        ts.error = "album download failed"
                return
            if done:
                break
            time.sleep(0.5)
        self._package_task(task_id, req, cache_hash)

    def _package_task(self, task_id: str, req: TaskRequest, cache_hash: str):
        try:
            existing = self._get_artifact(req.album_id, cache_hash, req.output_format)
            if existing:
                filename = existing
            else:
                filename = self._build_artifact(req.album_id, req, cache_hash)
            url = f"{self.static_route}/{req.album_id}/{filename}"
            with self._lock:
                ts = self.tasks.get(task_id)
                if ts:
                    ts.status = "done"
                    ts.stage = "done"
                    ts.artifact_filename = filename
                    ts.download_url = url
                    st = self.album_state.get(req.album_id)
                    if st:
                        ts.metadata.update(st.get("metadata", {}))
                        if st.get("total_images"):
                            ts.total_images = st["total_images"]
                        else:
                            imgs = None
                            try:
                                imgs = len(self._collect_images(req.album_id))
                            except Exception:
                                imgs = None
                            ts.total_images = imgs
                    pwd = self._get_cached_password(req.album_id, cache_hash)
                    if pwd:
                        ts.password = pwd
        except Exception as e:
            with self._lock:
                ts = self.tasks.get(task_id)
                if ts:
                    ts.status = "failed"
                    ts.error = str(e)

    def _download_album(self, req: TaskRequest):
        ws = self._ensure_workspace(req.album_id)
        dir_rule = JM_CFG.get("dir_rule", {}).copy()
        dir_rule["base_dir"] = str(ws)

        client_cfg = JM_CFG.get("client", {}).copy()
        postman = client_cfg.get("postman", {}) or {}
        meta = (postman.get("meta_data", {}) or {}).copy()

        if req.proxy is not None:
            meta["proxies"] = req.proxy
        else:
            cfg_proxy = JM_CFG.get("client", {}).get("postman", {}).get("meta_data", {}).get("proxies", None)
            meta["proxies"] = cfg_proxy

        option_yaml = {
            "log": JM_CFG.get("log", True),
            "client": {
                "impl": client_cfg.get("impl", "html"),
                "retry_times": client_cfg.get("retry_times", 5),
                "postman": {
                    "meta_data": meta
                },
                "domain": JM_CFG.get("domain", {})
            },
            "dir_rule": dir_rule,
            "download": JM_CFG.get("download", {})
        }

        yaml_str = yaml.safe_dump(option_yaml, allow_unicode=True, sort_keys=False)

        set_current_task_manager(self)

        try:
            album, downloader = download_album(req.album_id, create_option_by_str(yaml_str), True)  # type: ignore
        except TypeError:
            download_album(req.album_id, create_option_by_str(yaml_str))  # type: ignore

    def _hash_params(self, req: TaskRequest) -> str:
        payload = {
            "album_id": str(req.album_id),
            "output_format": req.output_format,
            "quality": req.quality,
            "encrypt": req.encrypt,
            "compression": req.compression,
            "password_hash": hashlib.sha256((req.password or "").encode("utf-8")).hexdigest() if req.password else None,
        }
        s = json.dumps(payload, sort_keys=True, ensure_ascii=False)
        return hashlib.sha256(s.encode("utf-8")).hexdigest()

    def _ensure_workspace(self, album_id: str) -> Path:
        ws = self.work_dir / str(album_id)
        ws.mkdir(parents=True, exist_ok=True)
        # Images are stored under titled subdirectories per photo (work/{album_id}/{photo_title}/...)
        return ws

    def _is_album_complete(self, album_id: str):
        """
        判断本地 workspace 是否已有完整下载，若是则返回 (True, meta) 并避免再次调用 API。
        完整性判定：
          - 存在 meta.json 且 meta["complete"] 为 True
          - 本地至少检索到 1 张图片
          - 如 meta 包含 page_count，则本地图片数 >= page_count
        """
        ws = self._ensure_workspace(album_id)
        meta_path = ws / "meta.json"
        try:
            meta = {}
            if meta_path.exists():
                meta = json.loads(meta_path.read_text(encoding="utf-8"))
            imgs = self._collect_images(album_id)
            if not imgs:
                return False, {}
            if not isinstance(meta, dict) or not meta.get("complete"):
                return False, {}
            page_count = meta.get("page_count")
            if isinstance(page_count, int) and page_count > 0 and len(imgs) < page_count:
                return False, meta
            return True, meta
        except Exception:
            return False, {}

    def _get_artifact(self, album_id: str, cache_hash: str, fmt: str) -> Optional[str]:
        album_dir = self.artifacts_dir / str(album_id)
        if not album_dir.exists():
            return None
        # try index mapping cache_hash -> filename
        index_path = album_dir / "artifact_index.json"
        if index_path.exists():
            try:
                mapping = json.loads(index_path.read_text(encoding="utf-8"))
                filename = mapping.get(cache_hash)
                if filename:
                    fpath = album_dir / filename
                    return filename if fpath.exists() else None
            except Exception:
                pass
        # fallback only for deterministic rules
        cfg = SERVER_CFG.get("artifact_name", {}) or {}
        rule = cfg.get("rule", "short_hash")
        if rule == "album_id":
            base = str(album_id)
        elif rule == "short_hash":
            base = cache_hash[: int(cfg.get("short_hash", {}).get("length", 8))]
        else:
            # random/date rules are non-deterministic; cannot reconstruct without index
            return None
        filename = f"{base}.{fmt}"
        fpath = album_dir / filename
        return filename if fpath.exists() else None

    def _cache_artifact(self, album_id: str, cache_hash: str, filename: str, password: Optional[str] = None):
        album_cache = self.artifact_cache.setdefault(str(album_id), {})
        album_cache[cache_hash] = filename
        if password:
            pwd_file_dir = self.artifacts_dir / str(album_id)
            pwd_file_dir.mkdir(parents=True, exist_ok=True)
            (pwd_file_dir / f"{cache_hash}.pwd").write_text(password, encoding="utf-8")

    def _get_cached_password(self, album_id: str, cache_hash: str) -> Optional[str]:
        pwd_file = self.artifacts_dir / str(album_id) / f"{cache_hash}.pwd"
        if pwd_file.exists():
            return pwd_file.read_text(encoding="utf-8")
        return None

    def _collect_images(self, album_id: str) -> List[Path]:
        ws = self._ensure_workspace(album_id)
        imgs: List[Path] = []
        for root, _, files in os.walk(ws):
            for fn in files:
                if fn.lower().endswith((".jpg", ".jpeg", ".png", ".webp")):
                    imgs.append(Path(root) / fn)

        # sort by filename (usually like p001.jpg, p002.jpg, ...)
        imgs.sort(key=lambda x: x.name)
        return imgs

    def _build_artifact(self, album_id: str, req: TaskRequest, cache_hash: str) -> str:
        images = self._collect_images(album_id)
        if not images:
            raise RuntimeError("no images found for packaging")
        album_dir = self.artifacts_dir / str(album_id)
        album_dir.mkdir(parents=True, exist_ok=True)

        password = req.password
        if req.encrypt and not password:
            password = generate_password()

        # compute artifact filename base according to config rule
        cfg = SERVER_CFG.get("artifact_name", {}) or {}
        rule = cfg.get("rule", "short_hash")
        if rule == "album_id":
            base = str(album_id)
        elif rule == "short_hash":
            base = cache_hash[: int(cfg.get("short_hash", {}).get("length", 8))]
        elif rule == "random":
            sub = cfg.get("random", {}) or {}
            length = int(sub.get("length", 8))
            charset = sub.get("charset", "ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz23456789")
            import secrets
            base = "".join(secrets.choice(charset) for _ in range(length))
        elif rule == "date":
            from datetime import datetime
            fmt = cfg.get("date", {}).get("format", "%Y%m%d")
            base = datetime.now().strftime(fmt)
        else:
            base = cache_hash[:8]

        if req.output_format == "zip":
            filename = f"{base}.zip"
            fpath = album_dir / filename
            self._make_zip(fpath, images, req, password)
        else:
            filename = f"{base}.pdf"
            fpath = album_dir / filename
            self._make_pdf(fpath, images, req, password)

        # persist index mapping for cache lookups
        index_path = album_dir / "artifact_index.json"
        try:
            mapping = {}
            if index_path.exists():
                mapping = json.loads(index_path.read_text(encoding="utf-8"))
            mapping[cache_hash] = filename
            index_path.write_text(json.dumps(mapping, ensure_ascii=False, indent=2), encoding="utf-8")
        except Exception:
            pass

        self._cache_artifact(album_id, cache_hash, filename, password if req.encrypt else None)
        return filename

    def _make_zip(self, fpath: Path, images: List[Path], req: TaskRequest, password: Optional[str]):
        import pyzipper
        mode = pyzipper.ZIP_DEFLATED
        with pyzipper.AESZipFile(fpath, 'w', compression=mode, compresslevel=req.compression) as zf:
            if req.encrypt and password:
                zf.setpassword(password.encode("utf-8"))
                zf.setencryption(pyzipper.WZ_AES, nbits=256)
            for img in images:
                arcname = img.name
                zf.write(img, arcname)

    def _make_pdf(self, fpath: Path, images: List[Path], req: TaskRequest, password: Optional[str]):
        import img2pdf
        from PIL import Image
        tmp_files: List[Path] = []
        try:
            for img in images:
                with Image.open(img) as im:
                    if im.mode in ("RGBA", "P"):
                        im = im.convert("RGB")
                    out = img
                    if req.quality is not None and img.suffix.lower() != ".jpg":
                        out = img.with_suffix(".jpg")
                        im.save(out, format="JPEG", quality=req.quality)
                        tmp_files.append(out)
                    else:
                        tmp_files.append(out)
            with open(fpath, "wb") as f:
                data = img2pdf.convert([str(p) for p in tmp_files])
                if data is None:
                    raise RuntimeError("img2pdf conversion failed")
                f.write(data)
        finally:
            pass
        if req.encrypt and password:
            import pikepdf
            with pikepdf.open(fpath, allow_overwriting_input=True) as pdf:
                pdf.save(fpath, encryption=pikepdf.Encryption(user=password, owner=password, R=6))

app = FastAPI(title="JMComic Download Server")

static_route = SERVER_CFG.get("static_route", "/artifacts")
app.mount(static_route, StaticFiles(directory=str(ARTIFACTS_DIR)), name="artifacts")

TM = TaskManager(ARTIFACTS_DIR, WORK_DIR, static_route)

@app.post("/tasks")
def submit_task(req: TaskRequest):
    if req.proxy is not None:
        if ":" not in req.proxy:
            raise HTTPException(status_code=400, detail="proxy must be host:port or null")
    status = TM.submit(req)
    return JSONResponse(status.model_dump())

@app.get("/tasks/{task_id}")
def get_task(task_id: str):
    st = TM.get(task_id)
    if not st:
        raise HTTPException(status_code=404, detail="task not found")
    return JSONResponse(st.model_dump())

@app.get("/tasks")
def list_tasks():
    return JSONResponse([t.model_dump() for t in TM.list_tasks()])

@app.get("/tasks/{task_id}/download/{filename}")
def download_link(task_id: str, filename: str):
    st = TM.get(task_id)
    if not st or not st.download_url or not st.artifact_filename:
        raise HTTPException(status_code=404, detail="artifact not found")
    if filename != st.artifact_filename:
        raise HTTPException(status_code=404, detail="filename mismatch")
    return {"download_url": st.download_url}

def start():
    import uvicorn
    host = SERVER_CFG.get("host", "0.0.0.0")
    port = int(SERVER_CFG.get("port", 8000))
    uvicorn.run("src.main:app", host=host, port=port, reload=False)

# Password generation policy driven by config (SERVER_CFG["password"])
def get_password_policy():
    """
    从配置 server.password 读取随机密码生成策略:
    - length: 密码长度 (默认 12)
    - charset: 字符集 (默认不含易混淆字符)
    """
    policy = SERVER_CFG.get("password", {}) or {}
    try:
        length = int(policy.get("length", 12))
    except Exception:
        length = 12
    charset = policy.get("charset", "ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz23456789@#-_")
    if not isinstance(charset, str) or not charset:
        charset = "ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz23456789@#-_"
    return length, charset

def generate_password() -> str:
    """
    根据 server.password 策略生成随机密码。
    不使用 uuid，避免包含不需要的字符；仅使用策略指定字符集。
    """
    import secrets
    length, charset = get_password_policy()
    return "".join(secrets.choice(charset) for _ in range(length))
