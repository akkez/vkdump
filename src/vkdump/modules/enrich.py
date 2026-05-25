"""Async media downloader.

For every attachment of the selected kinds whose download_status isn't
'ok' yet, fetch the URL and save it under `data/static/<kind>/<bucket>/`
with a content-addressed filename. Mark the row with the result.

- Validation: HTTP 2xx + magic-byte sniff + minimum size. Failures land
  with `download_status='failed'` and the reason in `download_error`,
  so a follow-up run can retry just the failures.
- Idempotency: rows already at `ok` and whose `local_path` still exists
  on disk are skipped. Rows at `ok` whose file vanished are re-queued.
- Concurrency: one shared aiohttp session + a semaphore. Per-host cap
  keeps us polite on individual CDN subdomains.
- Filename: `<sha256(url)>.<ext>` under `<kind>/<sha[:2]>/`. Same URL
  hits the same path, so duplicates across messages share one file.
"""
from __future__ import annotations

import asyncio
import hashlib
import os
import signal
import sqlite3
import ssl
import time
from collections import deque
from pathlib import Path

import aiohttp
import certifi
from loguru import logger

from ..core.db import connection
from ..core.progress import Cancelled, ProgressReporter

# A current desktop Chrome string — VK CDN sometimes 403s on python-requests
# / curl defaults. Real-looking UA is enough; no other headers needed.
DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_0) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)
DEFAULT_CONCURRENCY = 16
DEFAULT_PER_HOST = 8
DEFAULT_TIMEOUT = 20  # seconds per request
DEFAULT_KINDS: tuple[str, ...] = ("photo",)
MIN_VALID_BYTES = 128

STATUS_OK = "ok"
STATUS_FAILED = "failed"
STATUS_SKIPPED = "skipped"

# (start-bytes, file-suffix).
_IMAGE_MAGICS: list[tuple[bytes, str]] = [
    (b"\xff\xd8\xff", ".jpg"),
    (b"\x89PNG\r\n\x1a\n", ".png"),
    (b"GIF87a", ".gif"),
    (b"GIF89a", ".gif"),
]


def run(params: dict, progress: ProgressReporter) -> dict:
    kinds = tuple(params.get("kinds") or DEFAULT_KINDS)
    concurrency = int(params.get("concurrency") or DEFAULT_CONCURRENCY)
    per_host = int(params.get("per_host") or DEFAULT_PER_HOST)
    timeout_s = int(params.get("timeout") or DEFAULT_TIMEOUT)
    skipped_unsupported = _mark_unsupported_urls(kinds)
    if skipped_unsupported:
        progress.log(
            f"enrich-media: skipped {skipped_unsupported} on-site vk.com photo URLs"
        )
    rows = _pick_rows(kinds)
    if not rows:
        progress.log(f"enrich-media: nothing to download for kinds={list(kinds)}")
        return {
            "kinds": list(kinds),
            "selected": 0,
            "downloaded": 0,
            "failed": 0,
            "skipped": 0,
        }

    progress.log(
        f"enrich-media: {len(rows)} attachment(s) to fetch "
        f"(kinds={list(kinds)}, concurrency={concurrency}, per_host={per_host}, "
        f"timeout={timeout_s}s)"
    )

    static_root = _static_root()
    static_root.mkdir(parents=True, exist_ok=True)

    started = time.perf_counter()
    try:
        stats = asyncio.run(
            _download_all(
                rows=rows,
                static_root=static_root,
                concurrency=concurrency,
                per_host=per_host,
                timeout_s=timeout_s,
                progress=progress,
            )
        )
    except KeyboardInterrupt:
        # Fallback path. The in-coroutine SIGINT handler in
        # `_download_all` should normally catch Ctrl+C without raising
        # KeyboardInterrupt here at all, but on platforms where
        # `loop.add_signal_handler` isn't supported we may still see one.
        raise Cancelled()
    elapsed = round(time.perf_counter() - started, 2)
    progress.log(
        f"enrich-media: done in {elapsed}s, "
        f"ok={stats['ok']} failed={stats['failed']} skipped={stats['skipped']}, "
        f"total {_fmt_bytes(stats.get('bytes', 0))}"
    )
    return {
        "kinds": list(kinds),
        "selected": len(rows),
        "downloaded": stats["ok"],
        "failed": stats["failed"],
        "skipped": stats["skipped"],
        "bytes_downloaded": stats.get("bytes", 0),
        "elapsed_seconds": elapsed,
    }


# ---------- DB selection ----------


def _mark_unsupported_urls(kinds: tuple[str, ...]) -> int:
    """Re-stamp on-site vk.com photo rows as 'skipped' regardless of
    their current status (so prior 'failed' attempts from older code
    don't keep cluttering `vkdump logs downloads`).

    The actual queue-time exclusion lives in :func:`_pick_rows`; this
    only normalises the persisted state.
    """
    if not kinds or "photo" not in kinds:
        return 0
    with connection() as conn:
        cur = conn.execute(
            """
            UPDATE attachments
               SET download_status = ?,
                   download_attempted_at = CURRENT_TIMESTAMP,
                   download_error = ?
             WHERE kind = 'photo'
               AND url LIKE 'https://vk.com/%'
               AND download_status != ?
            """,
            (STATUS_SKIPPED, "on-site vk.com URL", STATUS_SKIPPED),
        )
        return cur.rowcount or 0


def _pick_rows(kinds: tuple[str, ...]) -> list[sqlite3.Row]:
    """Pick attachments that still need fetching.

    Filters at the SQL level by:

    - kind ∈ `kinds`
    - URL present
    - URL is **not** `https://vk.com/...` — those are on-site links that
      need auth / redirect handling, we don't ever want them in the
      queue regardless of their stored status.
    - `download_status` is **not** 'skipped' — explicit skips stay out
      until the user re-queues them by hand.

    Among the remaining rows, those at status='ok' whose `local_path`
    is still present on disk are dropped in Python; rows whose file
    vanished get rescheduled.
    """
    if not kinds:
        return []
    placeholders = ",".join("?" * len(kinds))
    static_root = _static_root()
    with connection() as conn:
        candidates = conn.execute(
            f"""
            SELECT id, kind, url, local_path, download_status
              FROM attachments
             WHERE kind IN ({placeholders})
               AND url IS NOT NULL
               AND url != ''
               AND url NOT LIKE 'https://vk.com/%'
               AND download_status != ?
            """,
            (*kinds, STATUS_SKIPPED),
        ).fetchall()
    out: list[sqlite3.Row] = []
    for r in candidates:
        if r["download_status"] == STATUS_OK:
            local = r["local_path"]
            if local and (static_root / local).is_file():
                continue
            # File vanished — retry.
        out.append(r)
    return out


# ---------- async core ----------


async def _download_all(
    rows: list[sqlite3.Row],
    static_root: Path,
    concurrency: int,
    per_host: int,
    timeout_s: int,
    progress: ProgressReporter,
) -> dict:
    sem = asyncio.Semaphore(concurrency)
    # Build an SSL context backed by certifi's CA bundle. Python.framework
    # on macOS ships with an empty default trust store (the
    # `Install Certificates.command` post-install step seeds certifi into
    # it, but it's easy to skip). aiohttp's `ssl=True` reuses
    # `ssl.create_default_context()` which would then trust nothing and
    # raise SSLCertVerificationError on every host. Wiring certifi
    # explicitly removes that whole class of failure.
    ssl_ctx = ssl.create_default_context(cafile=certifi.where())
    connector = aiohttp.TCPConnector(
        limit=concurrency, limit_per_host=per_host, ssl=ssl_ctx,
    )
    timeout = aiohttp.ClientTimeout(total=timeout_s)
    headers = {"User-Agent": DEFAULT_USER_AGENT, "Accept": "image/*,*/*;q=0.8"}

    stats = {STATUS_OK: 0, STATUS_FAILED: 0, STATUS_SKIPPED: 0}
    total = len(rows)
    done = 0
    bytes_total = 0
    # Sliding window for the live speed read-out. We push a (timestamp,
    # cumulative-bytes) sample on every completion and trim entries
    # older than SPEED_WINDOW_S. Speed = (last_bytes - first_bytes) /
    # (now - first_timestamp), so idle time decays the displayed speed
    # towards zero without us having to pump it from a background tick.
    speed_window: deque[tuple[float, int]] = deque()
    SPEED_WINDOW_S = 5.0
    speed_window.append((time.monotonic(), 0))

    # Ctrl+C handling: install a SIGINT handler on the running loop that
    # just sets an asyncio.Event. We never raise KeyboardInterrupt into
    # our coroutine, so there's no race between asyncio's runner-level
    # cleanup and our own draining — the main loop notices the event,
    # exits the as_completed iteration cleanly, and the finally drains
    # all in-flight tasks in a normal async context.
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    sigint_installed = False
    try:
        loop.add_signal_handler(signal.SIGINT, stop.set)
        sigint_installed = True
    except (NotImplementedError, RuntimeError):
        # Windows / non-main-thread loops fall back to the default
        # signal handler — cancellation will work via the existing
        # progress.check_cancelled() path instead.
        pass

    async with aiohttp.ClientSession(
        connector=connector, timeout=timeout, headers=headers
    ) as session:

        async def _one(row: sqlite3.Row) -> tuple[int, str, dict]:
            async with sem:
                return row["id"], row["url"], await _fetch_one(session, row, static_root, timeout_s)

        tasks = [asyncio.create_task(_one(r)) for r in rows]
        cancelled = False
        try:
            for fut in asyncio.as_completed(tasks):
                att_id, url, result = await fut
                _persist_result(att_id, url, result)
                stats[result["status"]] = stats.get(result["status"], 0) + 1
                done += 1
                bytes_total += int(result.get("file_size") or 0)
                now = time.monotonic()
                speed_window.append((now, bytes_total))
                cutoff = now - SPEED_WINDOW_S
                while len(speed_window) > 1 and speed_window[0][0] < cutoff:
                    speed_window.popleft()
                t0, b0 = speed_window[0]
                dt = max(now - t0, 0.001)
                bps = (bytes_total - b0) / dt
                progress.report(
                    done, total,
                    f"enrich-media: {done}/{total}  "
                    f"ok={stats[STATUS_OK]} failed={stats[STATUS_FAILED]} "
                    f"skipped={stats[STATUS_SKIPPED]}  "
                    f"total {_fmt_bytes(bytes_total)} / {_fmt_bytes(bps)}/s",
                )
                if stop.is_set():
                    cancelled = True
                    progress.log("enrich-media: Ctrl+C — draining in-flight downloads")
                    break
                try:
                    progress.check_cancelled()
                except Cancelled:
                    cancelled = True
                    break
        finally:
            if sigint_installed:
                try:
                    loop.remove_signal_handler(signal.SIGINT)
                except (NotImplementedError, RuntimeError):
                    pass
            for t in tasks:
                if not t.done():
                    t.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
    stats["bytes"] = bytes_total
    if cancelled:
        raise Cancelled()
    return stats


async def _fetch_one(
    session: aiohttp.ClientSession,
    row: sqlite3.Row,
    static_root: Path,
    timeout_s: int,
) -> dict:
    url = row["url"]
    if not url:
        return {"status": STATUS_SKIPPED, "error": "no url"}

    rel = _rel_path_for(url, row["kind"])
    abs_path = static_root / rel
    if abs_path.is_file() and abs_path.stat().st_size >= MIN_VALID_BYTES:
        return {
            "status": STATUS_OK,
            "local_path": str(rel),
            "file_size": abs_path.stat().st_size,
            "content_type": None,
        }

    try:
        async with session.get(url) as resp:
            ct = resp.headers.get("Content-Type")
            if resp.status >= 400:
                return {"status": STATUS_FAILED, "error": f"HTTP {resp.status}", "content_type": ct}
            data = await resp.read()
    except asyncio.CancelledError:
        raise
    except asyncio.TimeoutError:
        return {"status": STATUS_FAILED, "error": f"timeout after {timeout_s}s"}
    except aiohttp.ClientError as exc:
        return {"status": STATUS_FAILED, "error": f"{type(exc).__name__}: {exc}"}
    except Exception as exc:  # noqa: BLE001
        logger.exception("unexpected download failure")
        return {"status": STATUS_FAILED, "error": f"{type(exc).__name__}: {exc}"}

    if len(data) < MIN_VALID_BYTES:
        return {
            "status": STATUS_FAILED,
            "error": f"response too small ({len(data)} bytes)",
            "file_size": len(data),
            "content_type": ct,
        }
    if not _looks_like_image(data):
        return {
            "status": STATUS_FAILED,
            "error": "payload didn't sniff as a known image format",
            "file_size": len(data),
            "content_type": ct,
        }

    rel = _rel_path_for(url, row["kind"], data=data)
    abs_path = static_root / rel
    abs_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = abs_path.with_suffix(abs_path.suffix + ".part")
    tmp.write_bytes(data)
    os.replace(tmp, abs_path)
    return {
        "status": STATUS_OK,
        "local_path": str(rel),
        "file_size": len(data),
        "content_type": ct,
    }


def _persist_result(att_id: int, url: str | None, result: dict) -> None:
    if result["status"] == STATUS_FAILED:
        # Mirror to the file logger so the failure shows up in
        # vkdump.log / vkdump-errors.log on disk, not only in the DB.
        logger.warning(
            "download failed for attachment #{} {}: {}",
            att_id, url or "<no url>", result.get("error") or "?",
        )
    with connection() as conn:
        conn.execute(
            """
            UPDATE attachments
               SET download_status = ?,
                   download_attempted_at = CURRENT_TIMESTAMP,
                   download_error = ?,
                   local_path = COALESCE(?, local_path),
                   file_size = COALESCE(?, file_size),
                   content_type = COALESCE(?, content_type)
             WHERE id = ?
            """,
            (
                result["status"],
                result.get("error"),
                result.get("local_path"),
                result.get("file_size"),
                result.get("content_type"),
                att_id,
            ),
        )


# ---------- paths / validation ----------


def _fmt_bytes(n: float) -> str:
    """Decimal-SI byte formatter: 1000 = 1 KB. Returns short labels like
    `12.3 MB`. No binary IEC variants on purpose.
    """
    n = float(n)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1000 or unit == "TB":
            if unit == "B":
                return f"{int(n)} B"
            return f"{n:.1f} {unit}"
        n /= 1000.0
    return f"{n:.1f} TB"


def _static_root() -> Path:
    return Path(__file__).resolve().parents[3] / "data" / "static"


def _rel_path_for(url: str, kind: str, data: bytes | None = None) -> Path:
    sha = hashlib.sha256(url.encode("utf-8")).hexdigest()
    ext = _ext_from_bytes(data) if data is not None else _ext_from_url(url)
    return Path(kind) / sha[:2] / f"{sha}{ext}"


def _ext_from_url(url: str) -> str:
    cleaned = url.split("?", 1)[0].split("#", 1)[0]
    suffix = Path(cleaned).suffix.lower()
    if suffix in (".jpg", ".jpeg", ".png", ".gif", ".webp"):
        return ".jpg" if suffix == ".jpeg" else suffix
    return ".bin"


def _ext_from_bytes(data: bytes) -> str:
    for prefix, ext in _IMAGE_MAGICS:
        if data.startswith(prefix):
            return ext
    if data.startswith(b"RIFF") and len(data) >= 12 and data[8:12] == b"WEBP":
        return ".webp"
    return ".bin"


def _looks_like_image(data: bytes) -> bool:
    if len(data) < 12:
        return False
    for prefix, _ext in _IMAGE_MAGICS:
        if data.startswith(prefix):
            return True
    if data.startswith(b"RIFF") and data[8:12] == b"WEBP":
        return True
    return False
