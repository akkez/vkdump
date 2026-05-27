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
import email.utils
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
from ..core.progress import Cancelled, ProgressReporter, Throttle

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


VALID_STRATEGIES = (
    "newest-first",
    "oldest-first",
    "groups-first",
    "dms-first",
    "my-uploads-first",
)
DEFAULT_STRATEGY = "newest-first"


def run(params: dict, progress: ProgressReporter) -> dict:
    kinds = _coerce_kinds(params.get("kinds"))
    concurrency = int(params.get("concurrency") or DEFAULT_CONCURRENCY)
    per_host = int(params.get("per_host") or DEFAULT_PER_HOST)
    timeout_s = int(params.get("timeout") or DEFAULT_TIMEOUT)
    chat_scope = params.get("chat_scope")
    if chat_scope in ("", None):
        chat_scope = None
    else:
        chat_scope = str(chat_scope).strip() or None
    strategy = (params.get("strategy") or DEFAULT_STRATEGY).strip()
    if strategy not in VALID_STRATEGIES:
        progress.log(
            f"enrich-media: unknown strategy {strategy!r}, falling back to {DEFAULT_STRATEGY!r}"
        )
        strategy = DEFAULT_STRATEGY
    skipped_unsupported = _mark_unsupported_urls(kinds)
    if skipped_unsupported:
        progress.log(
            f"enrich-media: skipped {skipped_unsupported} on-site vk.com photo URLs"
        )
    rows, resolved = _pick_rows(kinds, chat_scope=chat_scope, strategy=strategy)
    if not rows:
        progress.log(
            f"enrich-media: nothing to download for kinds={list(kinds)} "
            f"(already resolved: {resolved})"
        )
        return {
            "kinds": list(kinds),
            "selected": 0,
            "already_resolved": resolved,
            "downloaded": 0,
            "failed": 0,
            "skipped": 0,
        }

    scope_note = f" chat_scope={chat_scope}" if chat_scope else ""
    strat_note = f" strategy={strategy}" if strategy != DEFAULT_STRATEGY else ""
    progress.log(
        f"enrich-media: {len(rows)} attachment(s) to fetch, "
        f"{resolved} already resolved "
        f"(kinds={list(kinds)}, concurrency={concurrency}, per_host={per_host}, "
        f"timeout={timeout_s}s{scope_note}{strat_note})"
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
                done_offset=resolved,
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
        "already_resolved": resolved,
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


def _pick_rows(
    kinds: tuple[str, ...],
    chat_scope: str | None = None,
    strategy: str = DEFAULT_STRATEGY,
) -> tuple[list[sqlite3.Row], int]:
    """Pick attachments that still need fetching.

    Filters at the SQL level by:

    - kind ∈ `kinds`
    - URL present
    - URL is **not** `https://vk.com/...` — those are on-site links that
      need auth / redirect handling, we don't ever want them in the
      queue regardless of their stored status.
    - `download_status` is **not** 'skipped' — explicit skips stay out
      until the user re-queues them by hand.
    - If `chat_scope` is set: restrict to attachments from messages
      whose chat has that `peer_id`.

    `strategy` controls ordering only — every queued attachment still
    gets downloaded eventually. The tiebreaker for the chat-type and
    my-uploads strategies is `m.id DESC` (newest first):

    - "newest-first":     `messages.id DESC` — the default.
    - "oldest-first":     `messages.id ASC`.
    - "groups-first":     `chats.type = 'group_chat'` first.
    - "dms-first":        `chats.type = 'dm'` first.
    - "my-uploads-first": `messages.sender_is_self = 1` first.

    Among the remaining rows, those at status='ok' whose `local_path`
    is still present on disk are dropped in Python; rows whose file
    vanished get rescheduled.

    Returns `(rows_to_do, resolved_count)`. `resolved_count` is the
    number of in-scope attachments that the SQL filter already
    excluded as either status='skipped' or status='ok' with a file on
    disk — caller uses it to seed the progress bar so a resumed run
    starts at the right percent instead of "1 / <remaining>".
    """
    if not kinds:
        return [], 0
    placeholders = ",".join("?" * len(kinds))
    static_root = _static_root()

    needs_chats = chat_scope is not None or strategy in ("groups-first", "dms-first")
    chat_join = " JOIN chats c ON c.id = m.chat_id" if needs_chats else ""
    scope_clause = " AND c.peer_id = ?" if chat_scope is not None else ""

    # messages always joined now — every strategy orders by m.id, either
    # directly (newest/oldest-first) or as the within-tier tiebreaker.
    # `skipped` rows are pulled too (so they can be counted toward
    # `resolved`) and partitioned out in Python below.
    sql = f"""
    SELECT a.id, a.kind, a.url, a.local_path, a.download_status
      FROM attachments a
      JOIN messages m ON m.id = a.message_id
      {chat_join}
     WHERE a.kind IN ({placeholders})
       AND a.url IS NOT NULL
       AND a.url != ''
       AND a.url NOT LIKE 'https://vk.com/%'
       {scope_clause}
    """
    args: list = [*kinds]
    if chat_scope is not None:
        args.append(chat_scope)

    if strategy == "newest-first":
        sql += " ORDER BY m.id DESC, a.id DESC"
    elif strategy == "oldest-first":
        sql += " ORDER BY m.id ASC, a.id ASC"
    elif strategy == "groups-first":
        sql += " ORDER BY (c.type = 'group_chat') DESC, m.id DESC, a.id DESC"
    elif strategy == "dms-first":
        sql += " ORDER BY (c.type = 'dm') DESC, m.id DESC, a.id DESC"
    elif strategy == "my-uploads-first":
        sql += " ORDER BY m.sender_is_self DESC, m.id DESC, a.id DESC"

    with connection() as conn:
        candidates = conn.execute(sql, args).fetchall()
    out: list[sqlite3.Row] = []
    resolved = 0
    for r in candidates:
        status = r["download_status"]
        if status == STATUS_SKIPPED:
            # Permanent skip (e.g. on-site vk.com URL); never retried,
            # but counts toward the denominator.
            resolved += 1
            continue
        if status == STATUS_OK:
            local = r["local_path"]
            if local and (static_root / local).is_file():
                resolved += 1
                continue
            # File vanished — retry.
        out.append(r)
    return out, resolved


# ---------- async core ----------


async def _download_all(
    rows: list[sqlite3.Row],
    static_root: Path,
    concurrency: int,
    per_host: int,
    timeout_s: int,
    progress: ProgressReporter,
    done_offset: int = 0,
) -> dict:
    """Worker-pool downloader. We push every row into an asyncio.Queue
    and spawn `concurrency` workers that drain it. Two reasons not to
    `create_task` per row up front:

    1. Creating 165k tasks just to have them all stuck on a semaphore is
       wasteful (each one keeps a coroutine object alive in the loop).
    2. Cancelling 165k tasks on Ctrl+C produces the "Task was destroyed
       but it is pending!" lava-flow no matter how carefully we drain —
       `asyncio.as_completed` keeps an internal queue of futures that
       isn't reliably cleaned up under cancellation.

    With a fixed worker pool the active task set stays small. Ctrl+C
    just sets a stop event; workers notice it after they finish (or
    time out on) their current fetch and exit normally. Second Ctrl+C
    removes our signal handler so the default Python behaviour (raise
    KeyboardInterrupt) kicks in — caller turns that into a Cancelled
    too, but at least it doesn't have to wait for the pool to drain.
    """
    # SSL context backed by certifi's bundle — Python.framework on macOS
    # ships with an empty default trust store, see commit 3d52048.
    ssl_ctx = ssl.create_default_context(cafile=certifi.where())
    connector = aiohttp.TCPConnector(
        limit=concurrency, limit_per_host=per_host, ssl=ssl_ctx,
    )
    timeout = aiohttp.ClientTimeout(total=timeout_s)
    headers = {"User-Agent": DEFAULT_USER_AGENT, "Accept": "image/*,*/*;q=0.8"}

    stats = {STATUS_OK: 0, STATUS_FAILED: 0, STATUS_SKIPPED: 0}
    # `done`/`total` are the bar-facing counters: they include the
    # rows that were already-resolved before this run started (`done_offset`),
    # so a resumed run picks up at 10001/30000 instead of 1/20000.
    total = len(rows) + done_offset
    done = done_offset
    bytes_total = 0
    speed_window: deque[tuple[float, int]] = deque()
    SPEED_WINDOW_S = 5.0
    speed_window.append((time.monotonic(), 0))

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    sigint_installed = False

    def _on_sigint() -> None:
        stop.set()
        # Drop our handler so the next Ctrl+C uses Python's default
        # SIGINT → KeyboardInterrupt path, giving the user a way out
        # if a fetch is wedged inside aiohttp's read.
        try:
            loop.remove_signal_handler(signal.SIGINT)
        except (NotImplementedError, RuntimeError):
            pass

    try:
        loop.add_signal_handler(signal.SIGINT, _on_sigint)
        sigint_installed = True
    except (NotImplementedError, RuntimeError):
        # Windows / non-main-thread loops fall back to the
        # progress.check_cancelled() path.
        pass

    queue: asyncio.Queue = asyncio.Queue()
    for row in rows:
        queue.put_nowait(row)

    # Shared throttle across all workers (asyncio = single-threaded, so
    # no lock needed). Skips most per-tick `progress.report` calls when
    # we're churning through small files, but still emits at least once
    # every ~250 ms so the bar stays alive.
    progress_throttle = Throttle()

    async with aiohttp.ClientSession(
        connector=connector, timeout=timeout, headers=headers,
    ) as session:

        async def _worker() -> None:
            nonlocal done, bytes_total
            while not stop.is_set():
                try:
                    row = queue.get_nowait()
                except asyncio.QueueEmpty:
                    return
                try:
                    result = await _fetch_one(session, row, static_root, timeout_s)
                except asyncio.CancelledError:
                    raise
                except Exception as exc:  # noqa: BLE001
                    logger.exception("worker crashed on attachment #{}", row["id"])
                    result = {
                        "status": STATUS_FAILED,
                        "error": f"{type(exc).__name__}: {exc}",
                    }
                _persist_result(row["id"], row["url"], result)
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
                if progress_throttle(done, total):
                    progress.report(
                        done, total,
                        f"ok={stats[STATUS_OK]} failed={stats[STATUS_FAILED]} "
                        f"skipped={stats[STATUS_SKIPPED]} · "
                        f"{_fmt_bytes(bytes_total)} @ {_fmt_bytes(bps)}/s",
                    )
                try:
                    progress.check_cancelled()
                except Cancelled:
                    stop.set()
                    return

        workers = [asyncio.create_task(_worker()) for _ in range(concurrency)]
        try:
            await asyncio.gather(*workers, return_exceptions=True)
        finally:
            if sigint_installed:
                try:
                    loop.remove_signal_handler(signal.SIGINT)
                except (NotImplementedError, RuntimeError):
                    pass

    stats["bytes"] = bytes_total
    if stop.is_set():
        progress.log("enrich-media: cancelled — in-flight fetches finished cleanly")
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

    last_modified_ts: float | None = None
    last_modified_raw: str | None = None
    try:
        async with session.get(url) as resp:
            ct = resp.headers.get("Content-Type")
            last_modified_raw = resp.headers.get("Last-Modified")
            last_modified_ts = _parse_http_date(last_modified_raw)
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
    # Preserve the server's Last-Modified as the file's mtime so the
    # local mirror reflects when the photo was actually uploaded to VK,
    # not when we happened to fetch it. atime tracks our access; only
    # mtime gets back-dated.
    if last_modified_ts is not None:
        try:
            atime = abs_path.stat().st_atime
            os.utime(abs_path, (atime, last_modified_ts))
        except OSError:
            logger.warning("could not set mtime for {}", abs_path)
    return {
        "status": STATUS_OK,
        "local_path": str(rel),
        "file_size": len(data),
        "content_type": ct,
        "remote_modified_at": _ts_to_iso(last_modified_ts),
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
                   content_type = COALESCE(?, content_type),
                   remote_modified_at = COALESCE(?, remote_modified_at)
             WHERE id = ?
            """,
            (
                result["status"],
                result.get("error"),
                result.get("local_path"),
                result.get("file_size"),
                result.get("content_type"),
                result.get("remote_modified_at"),
                att_id,
            ),
        )


# ---------- paths / validation ----------


def _parse_http_date(raw: str | None) -> float | None:
    """RFC 7231 IMF-fixdate → unix timestamp. Tolerant of None / garbage."""
    if not raw:
        return None
    try:
        dt = email.utils.parsedate_to_datetime(raw)
    except (TypeError, ValueError):
        return None
    if dt is None:
        return None
    return dt.timestamp()


def _ts_to_iso(ts: float | None) -> str | None:
    """Unix epoch → 'YYYY-MM-DD HH:MM:SS+00:00' for SQLite TIMESTAMP."""
    if ts is None:
        return None
    from datetime import datetime, timezone
    return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat(sep=" ")


def _coerce_kinds(raw) -> tuple[str, ...]:
    """Accept either a list/tuple of kind slugs or a comma/space-separated
    string. The GUI hands us the raw QLineEdit text ("photo"), and
    `tuple("photo")` would otherwise expand to ('p','h','o','t','o').
    """
    if raw is None or raw == "":
        return DEFAULT_KINDS
    if isinstance(raw, str):
        parts = [p.strip() for p in raw.replace(";", ",").split(",")]
        cleaned = tuple(p for p in parts if p)
        return cleaned or DEFAULT_KINDS
    return tuple(str(k).strip() for k in raw if str(k).strip()) or DEFAULT_KINDS


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
    from ..core.db import app_dir
    return app_dir() / "data" / "static"


_FILENAME_HEX_LEN = 16  # 64 bits of entropy — birthday collision risk is
                        # vanishingly small at the scale we're at and the
                        # shorter names keep filesystem listings sane.


def _rel_path_for(url: str, kind: str, data: bytes | None = None) -> Path:
    sha = hashlib.sha256(url.encode("utf-8")).hexdigest()
    ext = _ext_from_bytes(data) if data is not None else _ext_from_url(url)
    name = sha[:_FILENAME_HEX_LEN]
    return Path(kind) / sha[:2] / f"{name}{ext}"


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
