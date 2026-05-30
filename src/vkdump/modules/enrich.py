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
import re
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
from ..core.i18n import plural
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
# Pre-scan chunk size. Keyset-paginating by a.id in this many rows at a
# time keeps any single SQL round-trip short on multi-million-row tables,
# so progress can tick smoothly instead of stalling on one giant SELECT.
_SCAN_CHUNK = 10000

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

    # Persist this run's effective inputs so the form auto-prefills
    # them next time (GUI or CLI). Stored under enrich_media.last_*
    # in app_config; matching default_providers in the registry pick
    # them back up.
    try:
        from ..core import app_config
        # kinds normalises through _coerce_kinds: accept either a list
        # or a comma-separated string; we write the comma form so the
        # GUI text field (which feeds back a string) round-trips.
        app_config.set("enrich_media.last_kinds", ",".join(kinds))
        app_config.set("enrich_media.last_chat_scope", chat_scope or "")
        app_config.set("enrich_media.last_strategy", strategy)
        app_config.set("enrich_media.last_concurrency", str(concurrency))
        app_config.set("enrich_media.last_per_host", str(per_host))
        app_config.set("enrich_media.last_timeout", str(timeout_s))
    except Exception:  # noqa: BLE001
        pass

    # One-shot backfill: pre-scan skips OK rows, so anything enriched
    # before the resolution column landed never reaches _fetch_one and
    # keeps NULL resolution. Run the URL→size regex over them once here
    # via a SQLite user-function — single UPDATE, idempotent (re-runs
    # find zero NULL rows and no-op).
    if "photo" in kinds:
        try:
            n_filled = _backfill_resolutions_from_url()
            if n_filled:
                progress.log(
                    f"enrich-media: backfilled resolution for "
                    f"{plural(n_filled, 'already-OK row')} via URL regex"
                )
        except Exception as exc:  # noqa: BLE001
            logger.exception("enrich-media: resolution backfill failed: {}", exc)

    rows, resolved = _pick_rows(
        kinds, chat_scope=chat_scope, strategy=strategy, progress=progress,
    )
    # `_pick_rows` drove the main bar during the scan phase; the
    # downloader phase below will overwrite it with its own (done, total).
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
        f"enrich-media: {plural(len(rows), 'attachment')} to fetch, "
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


def _mark_vk_skipped_in_range(
    conn: sqlite3.Connection, id_lo: int, id_hi: int | None
) -> int:
    """Stamp on-site vk.com photo rows as 'skipped' in `(id_lo, id_hi]`
    (or `id_lo..end` when `id_hi is None`). Called from inside the
    pre-scan loop so each write is bounded to the chunk's id range
    instead of one upfront full-table UPDATE that used to stall the
    worker thread for ~700 ms before the bar moved.

    Cosmetic: the pre-scan SELECT already filters vk.com URLs out of
    the download queue regardless of their status. This just normalises
    `vkdump logs downloads` so they show as 'skipped' instead of NULL.
    """
    if id_hi is not None:
        cur = conn.execute(
            "UPDATE attachments"
            "   SET download_status = ?,"
            "       download_attempted_at = CURRENT_TIMESTAMP,"
            "       download_error = ?"
            " WHERE id > ? AND id <= ?"
            "   AND kind = 'photo'"
            "   AND url LIKE 'https://vk.com/%'"
            "   AND (download_status IS NULL OR download_status != ?)",
            (STATUS_SKIPPED, "on-site vk.com URL", id_lo, id_hi, STATUS_SKIPPED),
        )
    else:
        cur = conn.execute(
            "UPDATE attachments"
            "   SET download_status = ?,"
            "       download_attempted_at = CURRENT_TIMESTAMP,"
            "       download_error = ?"
            " WHERE id > ?"
            "   AND kind = 'photo'"
            "   AND url LIKE 'https://vk.com/%'"
            "   AND (download_status IS NULL OR download_status != ?)",
            (STATUS_SKIPPED, "on-site vk.com URL", id_lo, STATUS_SKIPPED),
        )
    return cur.rowcount or 0


def _pick_rows(
    kinds: tuple[str, ...],
    chat_scope: str | None = None,
    strategy: str = DEFAULT_STRATEGY,
    progress: ProgressReporter | None = None,
) -> tuple[list[sqlite3.Row], int]:
    """Pick attachments that still need fetching.

    Chunked, keyset-paginated scan: walks rows in ascending `a.id` order
    in `_SCAN_CHUNK`-sized batches. Each chunk reports progress against
    `MAX(attachments.id)` — a single index probe, cheap even on
    multi-million-row tables. The denominator is approximate (it counts
    every attachment, not just in-scope ones), but the bar tracks the
    cursor walking through the rowid range, so it fills smoothly to
    100% by the time the scan finishes. The user-visible cost: zero
    upfront wait before the first chunk lands.

    File existence is **not** checked here. Rows already at status='ok'
    are trusted and excluded from the queue; if a file vanished, a manual
    re-run with status reset is required to re-queue it. Status='skipped'
    rows are likewise excluded.

    Sort by `strategy` happens in Python after the scan.

    Filters:

    - kind ∈ `kinds`
    - URL present
    - URL is **not** `https://vk.com/...` — those are on-site links that
      need auth / redirect handling, we don't ever want them in the
      queue regardless of their stored status.
    - If `chat_scope` is set (the chats.id primary key as a string):
      restrict to attachments from messages with that `chat_id`.

    Strategies (Python sort):

    - "newest-first":     `messages.id DESC` — the default.
    - "oldest-first":     `messages.id ASC`.
    - "groups-first":     `chats.type = 'group_chat'` first.
    - "dms-first":        `chats.type = 'dm'` first.
    - "my-uploads-first": `messages.sender_is_self = 1` first.

    Returns `(rows_to_do, resolved_count)`. `resolved_count` is the
    number of in-scope candidates excluded as status ∈ {'ok','skipped'}.
    """
    if not kinds:
        return [], 0
    placeholders = ",".join("?" * len(kinds))

    # `chat_scope` is `chats.id` (primary key, unambiguous across
    # accounts). Filter via `m.chat_id` so we don't need the chats
    # join just for scoping — the join is only pulled in when the
    # strategy needs `c.type` (groups-first / dms-first).
    needs_chats = strategy in ("groups-first", "dms-first")
    chat_join = " JOIN chats c ON c.id = m.chat_id" if needs_chats else ""
    chat_select = ", c.type AS chat_type" if needs_chats else ""
    scope_clause = " AND m.chat_id = ?" if chat_scope is not None else ""

    chunk_sql = (
        "SELECT a.id, a.kind, a.url, a.local_path, a.download_status,"
        " m.id AS message_id, m.sender_is_self"
        f"{chat_select}"
        " FROM attachments a"
        " JOIN messages m ON m.id = a.message_id"
        f"{chat_join}"
        " WHERE a.id > ?"
        f" AND a.kind IN ({placeholders})"
        " AND a.url IS NOT NULL AND a.url != ''"
        " AND a.url NOT LIKE 'https://vk.com/%'"
        f"{scope_clause}"
        " ORDER BY a.id ASC LIMIT ?"
    )
    base_args: list = [*kinds]
    if chat_scope is not None:
        base_args.append(chat_scope)

    keep: list[sqlite3.Row] = []
    resolved = 0
    last_id = 0
    vk_marked = 0
    mark_vk = "photo" in kinds
    throttle = Throttle()

    with connection() as conn:
        max_id = conn.execute("SELECT COALESCE(MAX(id), 0) FROM attachments").fetchone()[0]
        max_id = int(max_id) or 1  # avoid div-by-zero in the bar

        if progress is not None:
            progress.report(0, max_id, "Pre-scan…")

        while True:
            range_lo = last_id
            rows = conn.execute(
                chunk_sql, [range_lo, *base_args, _SCAN_CHUNK],
            ).fetchall()
            if not rows:
                break
            for r in rows:
                last_id = r["id"]
                status = r["download_status"]
                if status == STATUS_OK or status == STATUS_SKIPPED:
                    resolved += 1
                    continue
                keep.append(r)
            # Inline the vk.com cosmetic mark for the id range we just
            # walked. Bounded by (range_lo, last_id] so each write is
            # small even on a multi-million-row table.
            if mark_vk:
                vk_marked += _mark_vk_skipped_in_range(conn, range_lo, last_id)
            if progress is not None and throttle(last_id, max_id):
                progress.report(
                    last_id, max_id,
                    f"pre-scan · queued {len(keep)} · resolved {resolved}",
                )
                progress.check_cancelled()

        # Tail: catch vk.com rows past the last matched id (chunk SELECT
        # excludes vk.com so it never advances `last_id` past them).
        if mark_vk:
            vk_marked += _mark_vk_skipped_in_range(conn, last_id, None)

    if vk_marked and progress is not None:
        progress.log(
            f"enrich-media: marked {vk_marked} on-site vk.com photo URLs as skipped"
        )
    if progress is not None:
        progress.report(
            max_id, max_id,
            f"pre-scan done · queued {len(keep)} · resolved {resolved}",
        )

    keep.sort(key=_strategy_sort_key(strategy))
    return keep, resolved


def _strategy_sort_key(strategy: str):
    """Build a sort key matching the SQL ORDER BY the old `_pick_rows`
    used. Negate ints to flip ascending → descending; chat_type / sender
    flags become 0/1 bucket prefixes so the strategy-tier wins, with
    `message_id DESC, attachment_id DESC` as the tiebreaker.
    """
    if strategy == "oldest-first":
        return lambda r: (r["message_id"], r["id"])
    if strategy == "groups-first":
        return lambda r: (
            0 if r["chat_type"] == "group_chat" else 1, -r["message_id"], -r["id"],
        )
    if strategy == "dms-first":
        return lambda r: (
            0 if r["chat_type"] == "dm" else 1, -r["message_id"], -r["id"],
        )
    if strategy == "my-uploads-first":
        return lambda r: (
            0 if r["sender_is_self"] else 1, -r["message_id"], -r["id"],
        )
    # newest-first (default)
    return lambda r: (-r["message_id"], -r["id"])


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
            "resolution": _resolution_from_url(url),
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
        "resolution": _resolution_from_url(url),
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
                   remote_modified_at = COALESCE(?, remote_modified_at),
                   resolution = COALESCE(?, resolution)
             WHERE id = ?
            """,
            (
                result["status"],
                result.get("error"),
                result.get("local_path"),
                result.get("file_size"),
                result.get("content_type"),
                result.get("remote_modified_at"),
                result.get("resolution"),
                att_id,
            ),
        )


# ---------- paths / validation ----------


# VK CDN URLs frequently encode the rendered image size in the query
# string, e.g. `?size=379x309&quality=96&sign=…&type=album`. Reading
# resolution from there is a regex match — no fetch, no Pillow, no
# decode pass. When it's not present we leave `resolution` NULL and a
# future pass (e.g. Pillow on the cached file) can fill it in.
_URL_SIZE_RE = re.compile(r"[?&]size=(\d{1,5})x(\d{1,5})", re.IGNORECASE)


def _resolution_from_url(url: str | None) -> str | None:
    if not url:
        return None
    m = _URL_SIZE_RE.search(url)
    if m is None:
        return None
    return f"{m.group(1)}x{m.group(2)}"


def _backfill_resolutions_from_url() -> int:
    """Sweep OK photo rows whose `resolution` is still NULL and try to
    extract it from the URL once. Runs as a single UPDATE with a
    Python-side user function registered on the connection — no per-row
    round-trips, no fetch, no decode pass. Returns the rowcount.

    Idempotent: subsequent calls UPDATE nothing because
    `resolution IS NULL` no longer matches the same rows.
    """
    with connection() as conn:
        # `create_function(name, narg, fn)` exposes `fn` as a scalar
        # SQL function on this connection only — perfect scope-limit
        # for a one-off bulk operation. `deterministic=True` lets
        # SQLite skip per-row re-invocation if the optimizer can prove
        # it (irrelevant here but cheap to set).
        conn.create_function("vk_url_size", 1, _resolution_from_url, deterministic=True)
        cur = conn.execute(
            "UPDATE attachments"
            "   SET resolution = vk_url_size(url)"
            " WHERE download_status = 'ok'"
            "   AND resolution IS NULL"
            "   AND url IS NOT NULL"
            "   AND vk_url_size(url) IS NOT NULL"
        )
        return cur.rowcount or 0


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
