from __future__ import annotations

import io
import os
import shutil
import time
from dataclasses import dataclass
import threading
from pathlib import Path
from typing import (
    Any,
    Dict,
    List,
    Optional,
    Tuple,
    Union,
    Any,
    Callable,
    Dict,
    Optional,
)
from jmcomic import JmAlbumDetail, JmDownloader
from jmcomic.jm_option import JmImageDetail
import img2pdf
import pyzipper
from PIL import Image

from jmcomic import (
    JmOption,
    create_option_by_file,
    create_option_by_str,
)
from .config import AppConfig


@dataclass
class DownloadParams:
    album_ids: List[Union[int, str]]
    output_format: str  # 'zip' | 'pdf'
    quality: Optional[int]  # 1..100 jpeg quality if provided; None keeps originals
    encrypt: bool
    password: Optional[str]
    compression: int  # 0..9
    proxy: Optional[str]
    option_file: Optional[Path]


@dataclass
class DownloadResult:
    album_ids: List[str]
    artifact_path: Path
    suffix: str
    metadata: Dict[str, Any]


class CustomJmDownloader(JmDownloader):
    """
    Extend JmDownloader to intercept before_album and surface album metadata
    to a callback for task progress/state updates.
    """

    def __init__(
        self,
        *args,
        on_album_meta: Optional[Callable[[Dict[str, Any]], None]] = None,
        on_image_event: Optional[Callable[[str, Dict[str, Any]], None]] = None,
        **kwargs,
    ):
        super().__init__(*args, **kwargs)
        self.on_album_meta = on_album_meta
        # on_image_event(stage, info): stage in {"before", "after"}
        # info keys may include album_id, image_id, page, url, path, filename
        self.on_image_event = on_image_event

    def before_album(self, album: JmAlbumDetail):
        try:
            meta: Dict[str, Any] = {
                "album_id": getattr(album, "album_id", getattr(album, "id", None)),
                "title": getattr(album, "name", getattr(album, "title", None)),
            }
            # authors and tags
            authors = getattr(album, "author", None) or getattr(album, "authors", None)
            if authors is not None:
                meta["authors"] = authors if isinstance(authors, list) else [authors]
            tags = getattr(album, "tags", None)
            if tags is not None:
                meta["tags"] = list(tags) if not isinstance(tags, str) else [tags]
            # chapter count if iterable
            try:
                meta["chapter_count"] = sum(1 for _ in album)
            except Exception:
                pass
            if self.on_album_meta:
                self.on_album_meta(meta)
        except Exception:
            pass
        return super().before_album(album)

    def before_image(self, image: JmImageDetail, img_save_path):
        try:
            if self.on_image_event:
                info: Dict[str, Any] = {
                    "album_id": getattr(image, "album_id", getattr(image, "aid", None)),
                    "image_id": getattr(
                        image,
                        "image_id",
                        getattr(image, "pid", getattr(image, "id", None)),
                    ),
                    "page": getattr(image, "page", getattr(image, "index", None)),
                    "url": getattr(image, "img_url", None),
                    "path": str(img_save_path),
                }
                try:
                    info["filename"] = Path(str(img_save_path)).name
                except Exception:
                    pass
                self.on_image_event("before", info)
        except Exception:
            pass
        return super().before_image(image, img_save_path)

    def after_image(self, image: JmImageDetail, img_save_path):
        ret = super().after_image(image, img_save_path)
        try:
            if self.on_image_event:
                info: Dict[str, Any] = {
                    "album_id": getattr(image, "album_id", getattr(image, "aid", None)),
                    "image_id": getattr(
                        image,
                        "image_id",
                        getattr(image, "pid", getattr(image, "id", None)),
                    ),
                    "page": getattr(image, "page", getattr(image, "index", None)),
                    "url": getattr(image, "img_url", None),
                    "path": str(img_save_path),
                }
                try:
                    info["filename"] = Path(str(img_save_path)).name
                except Exception:
                    pass
                self.on_image_event("after", info)
        except Exception:
            pass
        return ret


class Downloader:
    # Ensure at most one concurrent download per album within this process
    _album_locks: Dict[str, threading.Lock] = {}

    def __init__(self, work_dir: Path, app_cfg: Optional[AppConfig] = None) -> None:
        self.work_dir = work_dir
        self.app_cfg = app_cfg

    def _get_album_lock(self, album_id: str) -> threading.Lock:
        # Double-checked locking for creating per-album locks
        lock = self._album_locks.get(album_id)
        if lock is None:
            with threading.Lock():
                lock = self._album_locks.get(album_id)
                if lock is None:
                    lock = threading.Lock()
                    self._album_locks[album_id] = lock
        return lock

    def _build_option(self, params: DownloadParams, task_work: Path) -> JmOption:
        # Highest priority: provided option file
        if params.option_file and Path(params.option_file).exists():
            return create_option_by_file(str(params.option_file))

        # Next: use jmcomic config from app config if provided
        yml_dict: Dict[str, Any] = {}
        if self.app_cfg and self.app_cfg.jmcomic:
            yml_dict = dict(self.app_cfg.jmcomic)  # shallow copy

        # Ensure dir_rule.base_dir is under task work
        yml_dict.setdefault("dir_rule", {})
        yml_dict["dir_rule"]["base_dir"] = f"{task_work.as_posix()}/"
        yml_dict["dir_rule"].setdefault("rule", "Bd / Aname / Ptitle")

        # Apply proxy override if provided
        if params.proxy is not None:
            client = yml_dict.setdefault("client", {})
            postman = client.setdefault("postman", {})
            meta = postman.setdefault("meta_data", {})
            meta["proxies"] = params.proxy
        elif self.app_cfg and self.app_cfg.server.default_proxy:
            client = yml_dict.setdefault("client", {})
            postman = client.setdefault("postman", {})
            meta = postman.setdefault("meta_data", {})
            meta.setdefault("proxies", self.app_cfg.server.default_proxy)

        # Fallback defaults if config empty
        if not yml_dict:
            yml_dict = {
                "log": True,
                "client": {
                    "impl": "html",
                    "retry_times": 5,
                    "postman": {"meta_data": {"proxies": params.proxy or "system"}},
                },
                "download": {
                    "cache": True,
                    "image": {"decode": True, "suffix": None},
                    "threading": {"image": 16, "photo": 8},
                },
                "dir_rule": {
                    "base_dir": f"{task_work.as_posix()}/",
                    "rule": "Bd / Aname / Ptitle",
                },
            }

        import yaml as _yaml

        return create_option_by_str(
            _yaml.safe_dump(yml_dict, allow_unicode=True, sort_keys=False)
        )

    def _prefetch_metadata(
        self, op: JmOption, album_id: Union[int, str]
    ) -> Dict[str, Any]:
        meta: Dict[str, Any] = {}
        try:
            cl = op.new_jm_client()
            album = cl.get_album_detail(str(album_id))
            # Best-effort metadata extraction
            meta["album_id"] = str(getattr(album, "album_id", album_id))
            meta["title"] = getattr(album, "name", getattr(album, "title", None))
            tags = getattr(album, "tags", None)
            if isinstance(tags, (list, tuple)):
                meta["tags"] = list(tags)
            # Count chapters
            try:
                meta["chapter_count"] = sum(1 for _ in album)
            except Exception:
                meta["chapter_count"] = None
            # Authors if available
            auth = getattr(album, "author", None) or getattr(album, "authors", None)
            if auth is not None:
                meta["authors"] = auth if isinstance(auth, list) else [auth]
        except Exception as e:
            meta["prefetch_error"] = str(e)
        return meta

    def _collect_images(self, base_dir: Path) -> List[Path]:
        exts = {".jpg", ".jpeg", ".png", ".webp"}
        images: List[Path] = []
        for root, _, files in os.walk(base_dir):
            for f in files:
                p = Path(root) / f
                if p.suffix.lower() in exts:
                    images.append(p)
        images.sort()
        return images

    def _reencode_images(
        self, images: List[Path], quality: int, staging: Path
    ) -> List[Path]:
        staging.mkdir(parents=True, exist_ok=True)
        out: List[Path] = []
        for idx, src in enumerate(images, start=1):
            dst = staging / f"{idx:05d}.jpg"
            try:
                with Image.open(src) as im:
                    if im.mode in ("RGBA", "P"):
                        im = im.convert("RGB")
                    im.save(dst, format="JPEG", quality=quality, optimize=True)
                out.append(dst)
            except Exception:
                # Fallback: copy original
                shutil.copy2(src, dst)
                out.append(dst)
        return out

    def _make_zip(
        self, images_root: Path, output: Path, password: Optional[str], compression: int
    ) -> Path:
        output.parent.mkdir(parents=True, exist_ok=True)
        comp = pyzipper.ZIP_LZMA if compression >= 7 else pyzipper.ZIP_DEFLATED
        with pyzipper.AESZipFile(output, "w", compression=comp) as zf:
            if password:
                zf.setpassword(password.encode("utf-8"))
                zf.setencryption(pyzipper.WZ_AES, nbits=256)
            # Write tree preserving relative paths
            for root, _, files in os.walk(images_root):
                for f in files:
                    fp = Path(root) / f
                    arc = str(fp.relative_to(images_root)).replace("\\", "/")
                    zf.write(fp, arc)
        return output

    def _make_pdf(
        self, images: List[Path], output: Path, password: Optional[str]
    ) -> Path:
        output.parent.mkdir(parents=True, exist_ok=True)
        # Build PDF from images
        img_bytes = [p.read_bytes() for p in images]
        pdf_bytes = img2pdf.convert(img_bytes)
        if not password:
            output.write_bytes(pdf_bytes or b"")
            return output
        # Encrypt using pikepdf
        from pikepdf import Encryption, Permissions, Pdf

        tmp = output.with_suffix(".tmp.pdf")
        tmp.write_bytes(pdf_bytes or b"")
        try:
            with Pdf.open(str(tmp)) as pdf:
                pdf.save(
                    str(output),
                    encryption=Encryption(
                        user=password,
                        owner=password,
                        allow=Permissions(extract=False, print_lowres=True),
                    ),
                )
        finally:
            if tmp.exists():
                tmp.unlink(missing_ok=True)  # type: ignore[arg-type]
        return output

    def download_and_package(
        self, params: DownloadParams, progress_cb=None
    ) -> DownloadResult:
        task_work = self.work_dir
        task_work.mkdir(parents=True, exist_ok=True)
        # Use a shared base dir for downloads to maximize cache hits across tasks
        dl_base = (
            self.app_cfg.server.cache_dir / "work"
            if self.app_cfg is not None
            else task_work
        )
        dl_base.mkdir(parents=True, exist_ok=True)

        op = self._build_option(params, dl_base)
        album_ids = [str(a) for a in params.album_ids]

        all_meta: Dict[str, Any] = {"albums": []}

        # Use custom downloader to capture metadata in before_album
        def on_album_meta(meta: Dict[str, Any]) -> None:
            all_meta.setdefault("albums", []).append(meta)
            if progress_cb:
                progress_cb(0.1, f"meta {meta.get('album_id')}")

        # Collect images per album for packaging without re-scanning dirs
        images_by_album: Dict[str, List[Dict[str, Any]]] = {}

        def on_image_event(stage: str, info: Dict[str, Any]) -> None:
            aid = str(info.get("album_id") or "")
            if not aid:
                return
            if stage == "after":
                lst = images_by_album.setdefault(aid, [])
                lst.append(
                    {
                        "path": Path(info.get("path", "")),
                        "page": info.get("page"),
                        "filename": info.get("filename"),
                    }
                )
            if progress_cb and info.get("image_id") is not None:
                progress_cb(0.2, f"image {stage} {info.get('image_id')}")

        jd = CustomJmDownloader(
            op, on_album_meta=on_album_meta, on_image_event=on_image_event
        )

        # Download each album to the shared base directory using downloader
        for idx, aid in enumerate(album_ids, start=1):
            if progress_cb:
                progress_cb(
                    0.05 + 0.4 * idx / max(1, len(album_ids)), f"download {aid}"
                )
            # Prefer JmDownloader API; fall back to function if necessary
            lock = self._get_album_lock(aid)
            with lock:
                try:
                    jd.download_album(aid)  # type: ignore[attr-defined]
                except Exception:
                    # Fallback: library-level function
                    from jmcomic import download_album as _download_album

                    _download_album(aid, op)

        # Build ordered image list from callbacks (fallback to directory scan if empty)
        if progress_cb:
            progress_cb(0.5, "collect images")
        selected: List[Path] = []
        for aid in album_ids:
            items = images_by_album.get(aid, [])
            # Order by page if available; else by filename
            items.sort(key=lambda x: (x.get("page") is None, x.get("page") or 0, str(x.get("filename") or x.get("path"))))
            selected.extend([Path(it["path"]) for it in items if it.get("path")])
        if not selected:
            # Fallback: scan the shared base dir limited by album_ids heuristically
            # As a safe fallback, scan entire dl_base (may be slower once per cold start)
            selected = self._collect_images(dl_base)

        # Optionally re-encode for quality
        src_root = None  # When None, we will use per-file list
        images = selected
        if params.quality is not None:
            if progress_cb:
                progress_cb(0.55, f"re-encode quality={params.quality}")
            staging = task_work / "_reencoded"
            images = self._reencode_images(images, params.quality, staging)
            src_root = staging

        # Package
        ts_name = time.strftime("%Y%m%d_%H%M%S")
        base_name = f"JM_{'_'.join(album_ids)}_{ts_name}"
        if params.output_format == "zip":
            out = task_work / f"{base_name}.zip"
            if progress_cb:
                progress_cb(0.75, "zip")
            if src_root is not None:
                # We have a prepared directory (re-encoded); zip the tree
                self._make_zip(
                    src_root,
                    out,
                    params.password if params.encrypt else None,
                    params.compression,
                )
            else:
                # Create a lightweight staging with hardlinks to avoid copying
                stage = task_work / "_pack_stage"
                # Clean and recreate
                if stage.exists():
                    shutil.rmtree(stage, ignore_errors=True)
                stage.mkdir(parents=True, exist_ok=True)
                # Group by album id into subfolders for readability
                index = 1
                for aid in album_ids:
                    sub = stage / f"album_{aid}"
                    sub.mkdir(parents=True, exist_ok=True)
                    for it in images_by_album.get(aid, []):
                        p = it.get("path")
                        if not p:
                            continue
                        src = Path(p)
                        if not src.exists():
                            continue
                        # Ensure unique sequential names to preserve order
                        dst = sub / f"{index:05d}_{Path(it.get('filename') or src.name).name}"
                        try:
                            os.link(src, dst)
                        except Exception:
                            shutil.copy2(src, dst)
                        index += 1
                self._make_zip(
                    stage,
                    out,
                    params.password if params.encrypt else None,
                    params.compression,
                )
                # Cleanup packing stage
                shutil.rmtree(stage, ignore_errors=True)
            suffix = "zip"
            artifact = out
        else:
            out = task_work / f"{base_name}.pdf"
            if progress_cb:
                progress_cb(0.75, "pdf")
            img_list = images
            self._make_pdf(img_list, out, params.password if params.encrypt else None)
            suffix = "pdf"
            artifact = out

        if progress_cb:
            progress_cb(0.95, "finalize")

        # Best-effort cleanup of staging directory (keep originals for debug)
        return DownloadResult(
            album_ids=album_ids,
            artifact_path=artifact,
            suffix=suffix,
            metadata=all_meta,
        )
