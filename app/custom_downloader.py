from __future__ import annotations

from typing import Any, Callable, Dict, Optional

from jmcomic import JmAlbumDetail, JmDownloader
from jmcomic.jm_option import JmImageDetail


class CustomJmDownloader(JmDownloader):
    """
    Extend JmDownloader to intercept before_album and surface album metadata
    to a callback for task progress/state updates.
    """

    def __init__(self, *args, on_album_meta: Optional[Callable[[Dict[str, Any]], None]] = None, **kwargs):
        super().__init__(*args, **kwargs)
        self.on_album_meta = on_album_meta

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
        return super().before_image(image, img_save_path)
    
    def after_image(self, image: JmImageDetail, img_save_path):
        return super().after_image(image, img_save_path)