"""Module: fetch full VK data for the active account.

For every local message worth scraping it pulls the canonical VK payload
(forwards, reply chains, full attachment metadata) into ``api_messages``.

A message is "worth scraping" when it has at least one attachment that
is *not* a CDN-hosted photo — i.e. any attachment whose ``kind`` is not
``photo``, or whose URL domain equals ``vk.com``. Photo attachments on
``*.userapi.com`` (and other off-vk CDNs) are skipped because we already
have the file URL locally; everything else needs API enrichment to
recover the structured payload (forward chains, geo coords, poll
answers, wall posts, etc.).

Two strategies, picked via the ``strategy`` param:

* ``get-by-id`` — one ``messages.getById`` request per batch of 100 ids,
  issued sequentially through the rate limiter.
* ``execute`` — packs up to 25 sub-calls of ``messages.getById`` into a
  single ``execute`` request and runs ``threads`` such requests in
  parallel. Each ``execute`` counts as one request against the shared
  3 req/s limiter, so peak throughput is ``25 * 100 * 3 = 7500`` msgs/s
  in theory and bounded by VK's per-token cap in practice.

All strategies share one :class:`RateLimiter` (3 req/s) and back off on
VK error 6 with the schedule ``(2, 3, 5, 7, 10, 15)`` seconds.

A background ticker logs scrape throughput every 10 seconds as 1m / 5m /
15m moving averages (htop load-average style).
"""
from __future__ import annotations

import asyncio
import json
from typing import Iterator

from loguru import logger

from ..core import app_config
from ..core.db import connection
from ..core.progress import Cancelled, ProgressReporter, Throttle
from ..core.rate_limiter import RateLimiter
from ..core.throughput import Throughput
from ..core.vk_api import VKApi, VKApiError


_ACTIVE_KEY = "active_account_id"
_BATCH_SIZE = 100
_SCAN_CHUNK = 5000
_MAX_RPS = 3.0
_RATE_LIMITED_CODE = 6
_RATE_LIMIT_BACKOFF_S = (2, 3, 5, 7, 10, 15)
_THROUGHPUT_TICK_S = 10.0
_EXECUTE_MAX_CALLS = 25


def run(params: dict, progress: ProgressReporter) -> dict:
    strategy = (params.get("strategy") or "get-by-id").strip()
    if strategy not in ("get-by-id", "execute"):
        raise ValueError(f"unknown strategy: {strategy!r}")
    threads = max(1, int(params.get("threads") or 4))
    per_request = int(params.get("per_request") or _EXECUTE_MAX_CALLS)
    per_request = max(1, min(per_request, _EXECUTE_MAX_CALLS))

    active_vk_id = (app_config.get(_ACTIVE_KEY) or "").strip()
    if not active_vk_id:
        raise RuntimeError(
            "No active account — open Settings, pick an account, then Authorize."
        )

    with connection() as conn:
        row = conn.execute(
            "SELECT id, vk_access_token FROM accounts"
            " WHERE provider='vk' AND vk_id=?",
            (active_vk_id,),
        ).fetchone()
    if row is None:
        raise RuntimeError(f"Active account vk:{active_vk_id} is not in the database.")
    account_pk = int(row["id"])
    token = row["vk_access_token"]
    if not token:
        raise RuntimeError(
            "Active account has no VK API token — open Settings → Authorize."
        )

    return asyncio.run(
        _run_async(account_pk, active_vk_id, token, strategy, threads, per_request, progress)
    )


async def _run_async(
    account_pk: int,
    vk_account_id_text: str,
    token: str,
    strategy: str,
    threads: int,
    per_request: int,
    progress: ProgressReporter,
) -> dict:
    state = _RunState()
    limiter = RateLimiter(_MAX_RPS)
    ticker_stop = asyncio.Event()

    async with VKApi(token) as vk:
        try:
            me = await vk.users_get(
                fields=["screen_name", "first_name", "last_name"]
            )
        except VKApiError as e:
            raise RuntimeError(f"users.get failed: {e}") from e
        if me:
            u = me[0]
            label = f"{u.get('first_name','')} {u.get('last_name','')}".strip()
            progress.log(f"Authorized as {label} (id{u.get('id')})")
            logger.info("fetch-data: authorized as {} (id{})", label, u.get("id"))

        max_id = _max_message_id(vk_account_id_text)
        if max_id == 0:
            progress.log("No messages in DB for the active account.")
            return state.summary(max_id)
        progress.log(
            f"strategy={strategy}"
            + (f" threads={threads} per_request={per_request}" if strategy == "execute" else "")
        )
        progress.report(0, max_id, "scanning…")

        ticker = asyncio.create_task(_throughput_ticker(state, progress, ticker_stop))
        try:
            if strategy == "get-by-id":
                await _run_get_by_id(
                    vk, limiter, account_pk, vk_account_id_text, max_id, state, progress
                )
            else:
                await _run_execute(
                    vk, limiter, account_pk, vk_account_id_text, max_id,
                    threads, per_request, state, progress,
                )
        finally:
            ticker_stop.set()
            try:
                await ticker
            except Exception:  # noqa: BLE001
                pass

    progress.report(
        max_id, max_id,
        f"done — fetched={state.fetched} skipped={state.skipped} missing={state.missing}",
    )
    avg = state.throughput.averages()
    summary = (
        f"fetch-data done: strategy={strategy} fetched={state.fetched}"
        f" skipped={state.skipped} missing={state.missing}"
        f" scanned_up_to_id={state.last_id} max_id={max_id}"
        f" msg/s 1m/5m/15m = {avg.format()}"
    )
    progress.log(summary)
    logger.info(summary)
    return state.summary(max_id)


class _RunState:
    def __init__(self) -> None:
        self.fetched = 0
        self.missing = 0
        self.skipped = 0
        self.last_id = 0
        self.throughput = Throughput()

    def summary(self, max_id: int) -> dict:
        return {
            "fetched": self.fetched,
            "missing": self.missing,
            "skipped": self.skipped,
            "max_id": max_id,
        }


async def _throughput_ticker(
    state: _RunState, progress: ProgressReporter, stop: asyncio.Event
) -> None:
    """Emit msg/s 1m/5m/15m averages every _THROUGHPUT_TICK_S seconds."""
    while not stop.is_set():
        try:
            await asyncio.wait_for(stop.wait(), timeout=_THROUGHPUT_TICK_S)
            return
        except asyncio.TimeoutError:
            pass
        avg = state.throughput.averages()
        msg = (
            f"throughput msg/s 1m/5m/15m = {avg.format()}"
            f" (fetched={state.fetched} skipped={state.skipped} missing={state.missing})"
        )
        progress.log(msg)
        logger.info(msg)


async def _run_get_by_id(
    vk: VKApi,
    limiter: RateLimiter,
    account_pk: int,
    vk_account_id_text: str,
    max_id: int,
    state: _RunState,
    progress: ProgressReporter,
) -> None:
    throttle = Throttle()
    for pending_ids, chunk_last_id, chunk_skipped in _iter_pending_chunks(
        account_pk, vk_account_id_text
    ):
        progress.check_cancelled()
        state.skipped += chunk_skipped
        state.last_id = chunk_last_id
        progress.report(
            state.last_id, max_id,
            f"fetched={state.fetched} skipped={state.skipped} missing={state.missing}",
        )
        for batch in _split(pending_ids, _BATCH_SIZE):
            progress.check_cancelled()
            items = await _get_by_id_with_backoff(vk, limiter, batch, progress)
            got_ids = {int(m["id"]) for m in items if isinstance(m.get("id"), int)}
            state.missing += len(batch) - len(got_ids)
            if items:
                _persist_batch(account_pk, items)
                state.fetched += len(items)
                state.throughput.add(len(items))
            if throttle(state.last_id, max_id):
                progress.report(
                    state.last_id, max_id,
                    f"fetched={state.fetched} skipped={state.skipped} missing={state.missing}",
                )


async def _run_execute(
    vk: VKApi,
    limiter: RateLimiter,
    account_pk: int,
    vk_account_id_text: str,
    max_id: int,
    threads: int,
    per_request: int,
    state: _RunState,
    progress: ProgressReporter,
) -> None:
    """Pack up to ``per_request`` ``messages.getById`` calls per ``execute``
    request and run ``threads`` of them in parallel. The rate limiter
    enforces the 3 req/s cap regardless of thread count."""
    queue: asyncio.Queue[list[int] | None] = asyncio.Queue(maxsize=threads * 4)
    throttle = Throttle()

    async def producer() -> None:
        try:
            for pending_ids, chunk_last_id, chunk_skipped in _iter_pending_chunks(
                account_pk, vk_account_id_text
            ):
                progress.check_cancelled()
                state.skipped += chunk_skipped
                state.last_id = chunk_last_id
                if throttle(state.last_id, max_id):
                    progress.report(
                        state.last_id, max_id,
                        f"fetched={state.fetched} skipped={state.skipped} missing={state.missing}",
                    )
                for batch in _split(pending_ids, _BATCH_SIZE):
                    await queue.put(batch)
        finally:
            for _ in range(threads):
                await queue.put(None)

    async def worker() -> None:
        while True:
            sub_batches: list[list[int]] = []
            first = await queue.get()
            if first is None:
                return
            sub_batches.append(first)
            while len(sub_batches) < per_request:
                try:
                    nxt = queue.get_nowait()
                except asyncio.QueueEmpty:
                    break
                if nxt is None:
                    for _ in range(threads):
                        await queue.put(None)
                    break
                sub_batches.append(nxt)
            progress.check_cancelled()
            items, missing_in_call = await _execute_get_by_id_with_backoff(
                vk, limiter, sub_batches, progress
            )
            state.missing += missing_in_call
            if items:
                _persist_batch(account_pk, items)
                state.fetched += len(items)
                state.throughput.add(len(items))

    workers = [asyncio.create_task(worker()) for _ in range(threads)]
    prod = asyncio.create_task(producer())
    try:
        await asyncio.gather(prod, *workers)
    except BaseException:
        for t in (prod, *workers):
            if not t.done():
                t.cancel()
        for t in (prod, *workers):
            try:
                await t
            except (Cancelled, asyncio.CancelledError, Exception):
                pass
        raise


def _max_message_id(vk_account_id_text: str) -> int:
    with connection() as conn:
        row = conn.execute(
            "SELECT COALESCE(MAX(id), 0) AS m FROM messages"
            " WHERE provider='vk' AND account_id=? AND attachment_count > 0",
            (vk_account_id_text,),
        ).fetchone()
    return int(row["m"]) if row else 0


def _iter_pending_chunks(
    account_pk: int, vk_account_id_text: str
) -> Iterator[tuple[list[int], int, int]]:
    """Yield ``(pending_vk_ids, chunk_last_id, chunk_skipped)`` for each
    keyset slice of size up to ``_SCAN_CHUNK`` over the scrapeworthy
    messages.

    Three index-only stages per chunk:

    1. Pull (id, vk_message_id) from ``messages`` filtered by
       ``attachment_count > 0`` (partial index
       ``idx_messages_with_attachments``).
    2. For each candidate, check via ``attachments`` whether at least
       one attachment is not a non-vk-CDN photo. Done in one EXISTS
       subquery so the scan stays index-driven.
    3. Set-diff against ``api_messages`` to get the actually-pending
       ids; ``skipped`` is the rest of the qualifying slice.
    """
    last_id = 0
    while True:
        with connection() as conn:
            rows = conn.execute(
                """
                SELECT m.id, m.vk_message_id
                  FROM messages m
                 WHERE m.provider='vk'
                   AND m.account_id=?
                   AND m.attachment_count > 0
                   AND m.id > ?
                   AND EXISTS (
                     SELECT 1 FROM attachments a
                      WHERE a.message_id = m.id
                        AND NOT (
                              a.kind = 'photo'
                          AND a.url IS NOT NULL
                          AND a.url != ''
                          AND a.url NOT LIKE 'http://vk.com/%'
                          AND a.url NOT LIKE 'https://vk.com/%'
                        )
                   )
                 ORDER BY m.id ASC
                 LIMIT ?
                """,
                (vk_account_id_text, last_id, _SCAN_CHUNK),
            ).fetchall()
        if not rows:
            return
        slice_vk_ids = [int(r["vk_message_id"]) for r in rows]
        chunk_last_id = int(rows[-1]["id"])

        placeholders = ",".join("?" * len(slice_vk_ids))
        with connection() as conn:
            existing = conn.execute(
                f"SELECT vk_message_id FROM api_messages"
                f" WHERE account_id=? AND vk_message_id IN ({placeholders})",
                (account_pk, *slice_vk_ids),
            ).fetchall()
        cached = {int(r["vk_message_id"]) for r in existing}

        pending = [vk_id for vk_id in slice_vk_ids if vk_id not in cached]
        skipped = len(slice_vk_ids) - len(pending)
        yield pending, chunk_last_id, skipped
        last_id = chunk_last_id


def _split(seq: list[int], n: int) -> Iterator[list[int]]:
    for i in range(0, len(seq), n):
        yield seq[i:i + n]


async def _get_by_id_with_backoff(
    vk: VKApi, limiter: RateLimiter, ids: list[int], progress: ProgressReporter
) -> list[dict]:
    """One ``messages.getById`` call, paced by the shared limiter, with
    fixed-schedule backoff on VK error 6 (Too many requests)."""
    backoffs = list(_RATE_LIMIT_BACKOFF_S) + [None]
    for i, delay in enumerate(backoffs):
        await limiter.acquire()
        try:
            resp = await vk.call(
                "messages.getById", message_ids=ids, extended=True,
            )
        except VKApiError as e:
            if e.code != _RATE_LIMITED_CODE or delay is None:
                raise
            progress.log(
                f"VK rate limited (code 6); backing off {delay}s"
                f" (retry {i + 1}/{len(_RATE_LIMIT_BACKOFF_S)})"
            )
            logger.warning("messages.getById rate limited; sleep {}s", delay)
            await asyncio.sleep(delay)
            continue
        items = resp.get("items") if isinstance(resp, dict) else None
        if items is None:
            return []
        return [m for m in items if isinstance(m, dict)]
    raise RuntimeError("unreachable")


async def _execute_get_by_id_with_backoff(
    vk: VKApi,
    limiter: RateLimiter,
    batches: list[list[int]],
    progress: ProgressReporter,
) -> tuple[list[dict], int]:
    """Pack ``batches`` (≤ 25 sub-batches of ≤ 100 ids each) into one
    ``execute`` request. Returns ``(items, missing)``.

    Sub-calls that VK reports under ``execute_errors`` with code 6
    trigger a backoff and retry of the whole execute. Other per-sub
    errors are counted toward ``missing`` along with ids that simply
    weren't returned.
    """
    requested = sum(len(b) for b in batches)
    backoffs = list(_RATE_LIMIT_BACKOFF_S) + [None]
    for i, delay in enumerate(backoffs):
        code = _build_execute_code(batches)
        await limiter.acquire()
        try:
            response, errors = await vk.execute(code)
        except VKApiError as e:
            if e.code != _RATE_LIMITED_CODE or delay is None:
                raise
            progress.log(
                f"VK rate limited (code 6) on execute; backing off {delay}s"
                f" (retry {i + 1}/{len(_RATE_LIMIT_BACKOFF_S)})"
            )
            logger.warning("execute rate limited; sleep {}s", delay)
            await asyncio.sleep(delay)
            continue
        rate_limited = any(
            int(e.get("error_code") or 0) == _RATE_LIMITED_CODE for e in errors
        )
        if rate_limited and delay is not None:
            progress.log(
                f"VK rate limited (code 6) on execute sub-call; backing off {delay}s"
                f" (retry {i + 1}/{len(_RATE_LIMIT_BACKOFF_S)})"
            )
            logger.warning("execute sub-call rate limited; sleep {}s", delay)
            await asyncio.sleep(delay)
            continue
        items = _flatten_execute_response(response)
        return items, max(0, requested - len(items))
    raise RuntimeError("unreachable")


def _build_execute_code(batches: list[list[int]]) -> str:
    """Compose a VKScript snippet that issues one ``messages.getById``
    per batch and returns the array of responses.

    ``message_ids`` is passed as a comma-joined string — VKScript
    accepts the same input shape as REST params.
    """
    calls = []
    for batch in batches:
        ids = ",".join(str(i) for i in batch)
        calls.append(
            'API.messages.getById({"message_ids":"' + ids + '","extended":1})'
        )
    return "return [" + ",".join(calls) + "];"


def _flatten_execute_response(response: object) -> list[dict]:
    """Concatenate the ``items`` arrays from each sub-call response.

    A failed sub-call shows up as ``False`` (or missing ``items``) and
    contributes nothing — the caller treats the gap as ``missing``.
    """
    if not isinstance(response, list):
        return []
    out: list[dict] = []
    for slot in response:
        if not isinstance(slot, dict):
            continue
        items = slot.get("items")
        if not isinstance(items, list):
            continue
        for m in items:
            if isinstance(m, dict):
                out.append(m)
    return out


def _persist_batch(account_pk: int, items: list[dict]) -> None:
    rows = []
    for m in items:
        vk_id = m.get("id")
        peer_id = m.get("peer_id")
        if not isinstance(vk_id, int) or not isinstance(peer_id, int):
            continue
        attachments = m.get("attachments") or None
        rows.append(
            (
                account_pk,
                peer_id,
                vk_id,
                json.dumps(m, ensure_ascii=False),
                json.dumps(attachments, ensure_ascii=False) if attachments else None,
            )
        )
    if not rows:
        return
    with connection() as conn:
        conn.executemany(
            "INSERT OR IGNORE INTO api_messages"
            " (account_id, peer_id, vk_message_id, message_data, attachment_data)"
            " VALUES (?, ?, ?, ?, ?)",
            rows,
        )
