"""Discover an account's conversations and cache their complete API histories."""
from __future__ import annotations

import asyncio
import json
import sqlite3
import time
import uuid
from collections.abc import Callable, Iterable
from datetime import datetime

from loguru import logger

from ..core import app_config
from ..core.api_store import persist_messages
from ..core.db import connection, resolve_db_path
from ..core.progress import Cancelled, ProgressReporter, Throttle
from ..core.rate_limiter import RateLimiter
from ..core.vk_api import VKApi, VKApiError, VKApiTimeout
from .fetch_data import (
    _EXECUTE_MAX_CALLS, _EXECUTE_THREADS, _MAX_RPS, _TIMEOUT_RETRY_S,
    _cancellation_bridge, _rate_limit_delay, _sleep_until_stop,
)

PAGE_SIZE = 200
DEFAULT_CALLS_PER_EXECUTE = 15


def _log(progress: ProgressReporter, event: str, **fields) -> None:
    stamp = datetime.now().astimezone().isoformat(timespec="milliseconds")
    detail = " ".join(f"{key}={json.dumps(value, ensure_ascii=False)}" for key, value in fields.items())
    line = f"{stamp} {event}" + (f" | {detail}" if detail else "")
    progress.log(line)
    level = "ERROR" if event == "error" else "WARNING" if event in ("retry", "fallback") else "INFO"
    logger.log(level, "download-messages | {}", line)



def run(params: dict, progress: ProgressReporter) -> dict:
    threads = int(params.get("threads", _EXECUTE_THREADS))
    per_request = int(params.get("per_request", DEFAULT_CALLS_PER_EXECUTE))
    if threads < 1 or not 1 <= per_request <= _EXECUTE_MAX_CALLS:
        raise ValueError("Threads must be positive; calls per execute must be 1..25.")
    app_config.set("download_messages.last_threads", str(threads))
    app_config.set("download_messages.last_per_request", str(per_request))
    active = str(params.get("account") or app_config.get("active_account_id") or "")
    with connection() as conn:
        row = conn.execute(
            "SELECT id, vk_id, vk_access_token "
            "FROM accounts WHERE provider='vk' AND vk_id=?",
            (active,),
        ).fetchone()
    if not row or not row["vk_access_token"]:
        raise RuntimeError("Authorize a VK account in Start / Settings or run authorize in CLI.")
    _log(progress, "start", account=int(row["vk_id"]),
         threads=threads, calls_per_execute=per_request,
         log_file=str(resolve_db_path().parent / "vkdump.log"))
    return asyncio.run(_run_async(
        int(row["id"]), int(row["vk_id"]), row["vk_access_token"],
        threads, per_request, progress,
    ))


async def _request(vk, limiter, method, params, stop, progress):
    attempt = 0
    request_id = uuid.uuid4().hex[:8]
    while not stop.is_set():
        progress.check_cancelled()
        await limiter.acquire()
        if stop.is_set():
            break
        started = time.monotonic()
        try:
            response = await vk.call(method, **params)
            return response
        except VKApiTimeout as exc:
            _log(progress, "error", request_id=request_id, method=method, params=params, error=str(exc))
            delay = _TIMEOUT_RETRY_S
        except VKApiError as exc:
            _log(progress, "error", request_id=request_id, method=method, params=params,
                 elapsed_ms=int((time.monotonic() - started) * 1000),
                 code=exc.code, message=exc.message)
            if exc.code != 6:
                raise
            delay = _rate_limit_delay(attempt)
            attempt += 1
        _log(progress, "retry", request_id=request_id, method=method, delay_seconds=delay)
        await _sleep_until_stop(delay, stop)
    raise Cancelled()


async def _execute(vk, limiter, params, stop, progress):
    code = "return [" + ",".join(
        "API.messages.getHistory(" + json.dumps(p, separators=(",", ":")) + ")"
        for p in params
    ) + "];"
    attempt = 0
    request_id = uuid.uuid4().hex[:8]
    while not stop.is_set():
        progress.check_cancelled()
        await limiter.acquire()
        if stop.is_set():
            break
        started = time.monotonic()
        try:
            response, errors = await vk.execute(code)
        except VKApiTimeout as exc:
            _log(progress, "error", request_id=request_id, method="execute", error=str(exc))
            delay = _TIMEOUT_RETRY_S
        except VKApiError as exc:
            _log(progress, "error", request_id=request_id, method="execute",
                 elapsed_ms=int((time.monotonic() - started) * 1000),
                 code=exc.code, message=exc.message)
            if exc.code != 6:
                _log(progress, "fallback", request_id=request_id, method="messages.getHistory", calls=len(params))
                return [await _request(vk, limiter, "messages.getHistory", p, stop, progress)
                        for p in params]
            delay = _rate_limit_delay(attempt)
            attempt += 1
        else:
            if not any(int(e.get("error_code") or 0) == 6 for e in errors):
                slots = response if isinstance(response, list) else []
                result = []
                for i, p in enumerate(params):
                    slot = slots[i] if i < len(slots) else None
                    if not isinstance(slot, dict) or not isinstance(slot.get("items"), list):
                        _log(progress, "fallback", request_id=request_id, slot=i + 1,
                             method="messages.getHistory", params=p)
                        slot = await _request(vk, limiter, "messages.getHistory", p, stop, progress)
                    result.append(slot)
                return result
            delay = _rate_limit_delay(attempt)
            attempt += 1
        _log(progress, "retry", request_id=request_id, method="execute", delay_seconds=delay)
        await _sleep_until_stop(delay, stop)
    raise Cancelled()


async def _parallel(vk, limiter, jobs: Iterable[dict], consume: Callable,
                    threads, per_request, stop, progress):
    iterator = iter(jobs)

    async def worker():
        while not stop.is_set():
            progress.check_cancelled()
            batch = []
            for _ in range(per_request):
                try:
                    batch.append(next(iterator))
                except StopIteration:
                    break
            if not batch:
                return
            responses = await _execute(vk, limiter, batch, stop, progress)
            progress.check_cancelled()
            for job, response in zip(batch, responses, strict=True):
                consume(job, response)

    workers = [asyncio.create_task(worker()) for _ in range(threads)]
    try:
        await asyncio.gather(*workers)
        if stop.is_set():
            raise Cancelled()
    finally:
        for task in workers:
            if not task.done():
                task.cancel()
        await asyncio.gather(*workers, return_exceptions=True)


def _items(response: object) -> list[dict]:
    if not isinstance(response, dict) or not isinstance(response.get("items"), list):
        raise RuntimeError("VK returned an invalid items response.")
    if any(not isinstance(item, dict) for item in response["items"]):
        raise RuntimeError("VK returned an invalid item.")
    return response["items"]


async def _discover(vk, limiter, account_pk, discovery_id, stop, progress):
    seen: set[int] = set()
    offset = 0
    progress.report(0, 0, "Discovering conversations…")
    _log(progress, "phase", name="discover-conversations", filter="all")
    while True:
        response = await _request(vk, limiter, "messages.getConversations", {
            "offset": offset, "count": PAGE_SIZE, "filter": "all",
            "extended": 1, "fields": "screen_name,photo_100",
        }, stop, progress)
        items = _items(response)
        total = int(response["count"])
        if not items:
            if offset < total:
                raise RuntimeError(f"Conversation enumeration stopped at {offset}/{total}.")
            break
        peers = {int(item["conversation"]["peer"]["id"]) for item in items}
        if peers <= seen:
            raise RuntimeError("VK repeated a conversation page; enumeration is incomplete.")
        _save_dialogs(account_pk, items, response, discovery_id)
        seen.update(peers)
        offset += len(items)
        progress.report(offset, max(total, offset), f"Discovering conversations: {len(seen)} saved")
        if offset >= total:
            break
    if len(seen) < total:
        raise RuntimeError("Conversation list changed during enumeration; run again to rescan.")
    _log(progress, "discovery-complete", dialogs=len(seen))
    return len(seen)


def _save_dialogs(account_pk, items, response, discovery_id):
    profiles = {int(p["id"]): p for p in response.get("profiles", [])}
    groups = {-int(g["id"]): g for g in response.get("groups", [])}
    with connection() as conn:
        from ..core.message_store import upsert_chat
        from ..parsers.vk.api import save_profiles
        save_profiles(conn,response)
        for item in items:
            conv = item["conversation"]
            peer = conv["peer"]
            peer_id = int(peer["id"])
            identity = profiles.get(peer_id) or groups.get(peer_id)
            title = (conv.get("chat_settings") or {}).get("title")
            if not title and identity:
                title = identity.get("name") or " ".join(
                    identity.get(k, "") for k in ("first_name", "last_name")
                ).strip()
            upsert_chat(conn,account_pk,peer_id,title=title,
                        type_={'chat':'group_chat','group':'community','user':'dm'}.get(peer['type'],peer['type']))
            last = item.get("last_message") or {}
            conn.execute(
                "INSERT INTO api_dialogs (account_id, peer_id, peer_type, title, "
                "conversation_data, identity_data, latest_message_id, last_message_at, discovery_id) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(account_id, peer_id) DO UPDATE SET "
                "peer_type=excluded.peer_type, title=excluded.title, "
                "conversation_data=excluded.conversation_data, "
                "identity_data=COALESCE(excluded.identity_data, api_dialogs.identity_data), "
                "latest_message_id=excluded.latest_message_id, "
                "last_message_at=excluded.last_message_at, discovery_id=excluded.discovery_id, "
                "discovered_at=CURRENT_TIMESTAMP",
                (account_pk, peer_id, peer["type"], title, json.dumps(item, ensure_ascii=False),
                 json.dumps(identity, ensure_ascii=False) if identity else None,
                 last.get("id"), last.get("date"), discovery_id),
            )


def _history_params(peer_id, anchor=0, offset=0):
    params = {"peer_id": peer_id, "offset": offset, "count": PAGE_SIZE,
              "rev": 0, "extended": 1}
    if anchor:
        params["start_message_id"] = anchor
    return params


def _save_page(account_pk, job, response, expected):
    items = _items(response)
    peer_id = job["peer_id"]
    anchor = job.get("start_message_id") or (items[0]["id"] if items else 0)
    ids = [m.get("id") for m in items]
    if (any(type(mid) is not int for mid in ids) or len(set(ids)) != len(ids)
            or any(m.get("peer_id") != peer_id for m in items)):
        raise RuntimeError(f"Invalid message identity in history for peer {peer_id}.")
    persist_messages(account_pk, items, response)
    if len(items) != expected:
        raise RuntimeError(f"Incomplete history for peer {peer_id}, offset {job['offset']}: "
                           f"expected {expected}, received {len(items)}. Run again to rescan.")
    dates = [m["date"] for m in items if isinstance(m.get("date"), int)]
    with connection() as conn:
        conn.execute("DELETE FROM api_history_items "
                     "WHERE account_id=? AND peer_id=? AND anchor_id=? AND page_offset=?",
                     (account_pk, peer_id, anchor, job["offset"]))
        try:
            conn.executemany(
                "INSERT INTO api_history_items "
                "(account_id, peer_id, anchor_id, vk_message_id, page_offset) VALUES (?, ?, ?, ?, ?)",
                [(account_pk, peer_id, anchor, mid, job["offset"]) for mid in ids],
            )
        except sqlite3.IntegrityError as exc:
            raise RuntimeError(f"Overlapping history pages for peer {peer_id}; run again to rescan.") from exc
        conn.execute(
            "INSERT OR REPLACE INTO api_history_pages "
            "(account_id, peer_id, anchor_id, page_offset, message_count) VALUES (?, ?, ?, ?, ?)",
            (account_pk, peer_id, anchor, job["offset"], len(items)),
        )
        if dates:
            conn.execute(
                "UPDATE api_dialogs SET first_message_at=MIN(COALESCE(first_message_at, ?), ?), "
                "last_message_at=MAX(COALESCE(last_message_at, ?), ?) "
                "WHERE account_id=? AND peer_id=?",
                (min(dates), min(dates), max(dates), max(dates), account_pk, peer_id),
            )
    return anchor


def _dialog_rows(account_pk, discovery_id):
    with connection() as conn:
        return conn.execute(
            "SELECT peer_id, latest_message_id, history_anchor_id, message_count "
            "FROM api_dialogs WHERE account_id=? AND discovery_id=? ORDER BY peer_id",
            (account_pk, discovery_id),
        ).fetchall()


def _page_jobs(account_pk, discovery_id):
    for row in _dialog_rows(account_pk, discovery_id):
        with connection() as conn:
            done = {r[0] for r in conn.execute(
                "SELECT page_offset FROM api_history_pages "
                "WHERE account_id=? AND peer_id=? AND anchor_id=?",
                (account_pk, row["peer_id"], row["history_anchor_id"]),
            )}
        for offset in range(0, row["message_count"], PAGE_SIZE):
            if offset not in done:
                yield _history_params(row["peer_id"], row["history_anchor_id"], offset)


async def _run_async(account_pk, vk_id, token, threads, per_request, progress):
    stop = asyncio.Event()
    limiter = RateLimiter(_MAX_RPS, stop=stop)
    bridge = asyncio.create_task(_cancellation_bridge(progress, stop))
    discovery_id = uuid.uuid4().hex
    fetched = 0
    try:
        async with VKApi(token) as vk:
            me = await _request(vk, limiter, "users.get", {}, stop, progress)
            if not me or int(me[0]["id"]) != vk_id:
                raise RuntimeError("The VK token belongs to a different account.")
            dialogs = await _discover(vk, limiter, account_pk, discovery_id, stop, progress)
            counted = 0
            _log(progress, "phase", name="count-histories", dialogs=dialogs)

            def prepare(job, response):
                nonlocal counted, fetched
                items = _items(response)
                count = max(0, int(response["count"]) - int(response.get("skipped", 0)))
                anchor = job.get("start_message_id") or (items[0]["id"] if items else 0)
                with connection() as conn:
                    previous = conn.execute(
                        "SELECT history_anchor_id, message_count FROM api_dialogs "
                        "WHERE account_id=? AND peer_id=?", (account_pk, job["peer_id"]),
                    ).fetchone()
                    if previous["history_anchor_id"] != anchor or previous["message_count"] != count:
                        conn.execute("DELETE FROM api_history_items WHERE account_id=? AND peer_id=?",
                                     (account_pk, job["peer_id"]))
                        conn.execute("DELETE FROM api_history_pages WHERE account_id=? AND peer_id=?",
                                     (account_pk, job["peer_id"]))
                        conn.execute("UPDATE api_dialogs SET first_message_at=NULL WHERE account_id=? AND peer_id=?",
                                     (account_pk, job["peer_id"]))
                    conn.execute(
                        "UPDATE api_dialogs SET message_count=?, history_anchor_id=?, history_status='pending' "
                        "WHERE account_id=? AND peer_id=?", (count, anchor, account_pk, job["peer_id"]),
                    )
                with connection() as conn:
                    conn.execute("UPDATE chats SET total_expected_count=?,history_status='partial' WHERE account_id=? AND peer_id=?",(count,account_pk,str(job['peer_id'])))
                _save_page(account_pk, job, response, min(PAGE_SIZE, count))
                fetched += len(items)
                counted += 1
                progress.report(counted, dialogs, f"Counting histories: {counted}/{dialogs} conversations")

            probes = (_history_params(row["peer_id"], row["latest_message_id"])
                      for row in _dialog_rows(account_pk, discovery_id))
            await _parallel(vk, limiter, probes, prepare, threads, per_request, stop, progress)
            rows = _dialog_rows(account_pk, discovery_id)
            totals = {r["peer_id"]: r["message_count"] for r in rows}
            total = sum(totals.values())
            with connection() as conn:
                completed = conn.execute(
                    "SELECT COALESCE(SUM(p.message_count), 0) FROM api_history_pages p "
                    "JOIN api_dialogs d ON d.account_id=p.account_id AND d.peer_id=p.peer_id "
                    "AND d.history_anchor_id=p.anchor_id WHERE d.account_id=? AND d.discovery_id=?",
                    (account_pk, discovery_id),
                ).fetchone()[0]
            throttle = Throttle()
            progress.report(completed, total, f"Downloading messages: {completed}/{total}")
            _log(progress, "phase", name="download-histories", messages=total,
                 checkpoint_messages=completed, remaining=total - completed)

            def consume(job, response):
                nonlocal completed, fetched
                expected = min(PAGE_SIZE, totals[job["peer_id"]] - job["offset"])
                _save_page(account_pk, job, response, expected)
                fetched += expected
                completed += expected
                if throttle(completed, total):
                    progress.report(completed, total, f"Downloading messages: {completed}/{total}")

            await _parallel(vk, limiter, _page_jobs(account_pk, discovery_id), consume,
                            threads, per_request, stop, progress)
            progress.check_cancelled()
            if completed != total:
                raise RuntimeError(f"Incomplete history: {completed}/{total} messages.")
            with connection() as conn:
                conn.execute("UPDATE api_dialogs SET history_status='complete' "
                             "WHERE account_id=? AND discovery_id=?", (account_pk, discovery_id))
                conn.execute("UPDATE chats SET history_status=CASE WHEN message_count >= total_expected_count THEN 'complete' ELSE 'partial' END WHERE account_id=? AND peer_id IN (SELECT CAST(peer_id AS TEXT) FROM api_dialogs WHERE account_id=? AND discovery_id=?)",(account_pk,account_pk,discovery_id))
                conn.execute("DELETE FROM chat_data_issues WHERE source='api' AND field='history' AND chat_id IN (SELECT id FROM chats WHERE account_id=? AND history_status='complete')",(account_pk,))
                flagged = conn.execute("SELECT COUNT(*) FROM chat_data_issues i JOIN chats c ON c.id=i.chat_id WHERE c.account_id=?",(account_pk,)).fetchone()[0]
            progress.report(total, total, f"Done: {dialogs} conversations, {total} messages")
            _log(progress, "complete", dialogs=dialogs, messages=total, fetched=fetched)
            return {"dialogs": dialogs, "messages": total, "fetched": fetched, "flagged_fields": flagged}
    except Cancelled:
        _log(progress, "cancelled", fetched=fetched, checkpoints="saved")
        raise
    except Exception as exc:
        _log(progress, "error", phase="download-messages", error=str(exc),
             fetched=fetched, checkpoints="saved")
        with connection() as conn:
            conn.execute("INSERT OR REPLACE INTO chat_data_issues(chat_id,vk_message_id,source,field,reason) SELECT c.id,0,'api','history','history_unavailable' FROM chats c JOIN api_dialogs d ON d.account_id=c.account_id AND CAST(d.peer_id AS TEXT)=c.peer_id WHERE d.account_id=? AND d.discovery_id=? AND d.history_status!='complete'",(account_pk,discovery_id))
        raise
    finally:
        stop.set()
        await bridge
