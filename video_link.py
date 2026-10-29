"""Deliver finished videos as a stable /v/<task>.mp4 link that redirects to the upstream CDN.

DOLA_VIDEO_DELIVERY=link: the server no longer downloads the video. The task's video_url is
PUBLIC_BASE/v/<task_id>.mp4; each request is answered with a 302 to the upstream H.264 file,
so the bytes go from Dola's CDN to the client. The signed H.264 URL lives about six hours
(2026-10-29: the expiry is the 8-hex-digit path segment), so the stored video_model is used
to sign a fresh one shortly before it expires. When no H.264 URL can be obtained or the CDN
cannot be reached without the account proxy, the worker falls back to downloading.
"""
import asyncio
import json
import re
import time
from urllib.parse import urlsplit

import config

REFRESH_MARGIN = 15 * 60          # sign a new URL when less than this is left
_EXPIRY_SEGMENT = re.compile(r"^[0-9a-f]{8}$")
_refresh_locks: dict[str, asyncio.Lock] = {}


def enabled() -> bool:
    return config.VIDEO_DELIVERY == "link"


def link_expiry(url: str) -> float | None:
    """Expiry encoded in the CDN path (/<sig>/<hex unix time>/video/...), or None."""
    now = time.time()
    for segment in urlsplit(url or "").path.split("/"):
        if _EXPIRY_SEGMENT.fullmatch(segment):
            value = int(segment, 16)
            if now - 86400 < value < now + 20 * 365 * 86400:
                return float(value)
    return None


def probe_direct(url: str) -> dict | None:
    """Fetch 2 bytes without any proxy: the client will not use the account's proxy either."""
    import requests
    try:
        with requests.Session() as session:
            session.trust_env = False
            response = session.get(url, headers={"Range": "bytes=0-1"}, timeout=(8, 12), stream=True)
            with response:
                if response.status_code not in (200, 206):
                    return None
                if not str(response.headers.get("Content-Type", "")).startswith("video/"):
                    return None
                total = re.search(r"/(\d+)$", response.headers.get("Content-Range", ""))
                size = int(total.group(1)) if total else int(response.headers.get("Content-Length") or 0)
                return {"size": size or None, "expires_at": link_expiry(url)}
    except Exception:
        return None


def meta_from_model(video_model: str, size: int | None, duration: int | None, ratio: str | None) -> dict:
    """Task metadata from the upstream video_model, matching server._video_metadata's keys."""
    meta = {"video_size": size, "video_width": None, "video_height": None,
            "video_duration": None, "video_bitrate": None}
    try:
        vm = json.loads(video_model or "{}")
        best = max((v for v in (vm.get("video_list") or {}).values() if isinstance(v, dict)),
                   key=lambda v: int(v.get("vwidth") or 0), default={})
        meta["video_width"] = int(best.get("vwidth") or 0) or None
        meta["video_height"] = int(best.get("vheight") or 0) or None
        seconds = float(vm.get("video_duration") or best.get("duration") or 0)
        meta["video_duration"] = round(seconds, 2) if seconds else None
    except (ValueError, TypeError):
        pass
    if not meta["video_duration"] and duration:
        meta["video_duration"] = float(duration)
    if size and meta["video_duration"]:
        meta["video_bitrate"] = int(size * 8 / meta["video_duration"])
    return meta


async def current_link(row: dict, store) -> str | None:
    """The upstream URL for a link-delivered task, re-signed shortly before it expires."""
    link = row.get("video_link") or ""
    expires = float(row.get("video_link_expires_at") or 0)
    if link and (not expires or expires - time.time() > REFRESH_MARGIN):
        return link
    lock = _refresh_locks.setdefault(row["id"], asyncio.Lock())
    async with lock:
        fresh = store.get(row["id"]) or row
        if fresh.get("video_link") != link and fresh.get("video_link"):
            return fresh["video_link"]          # another request already re-signed it
        from video_worker_ui import fetch_higher_bitrate_url
        try:
            new = await fetch_higher_bitrate_url(row.get("video_model") or "", row.get("account"))
        except Exception:
            new = None
        if not new:
            return link or None                 # keep serving the old one rather than nothing
        store.update(row["id"], video_link=new, video_link_expires_at=link_expiry(new))
        return new
