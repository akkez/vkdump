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
import time
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
    stop = asyncio.Event()
    limiter = RateLimiter(_MAX_RPS, stop=stop)

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

        progress.report(0, 1, "pre-scan: counting eligible messages…")
        eligible = _count_eligible_messages(vk_account_id_text)
        state.eligible_total = eligible
        if eligible == 0:
            progress.log("No eligible messages for the active account.")
            return state.summary()
        progress.log(
            f"pre-scan: {eligible} eligible messages — strategy={strategy}"
            + (f" threads={threads} per_request={per_request}" if strategy == "execute" else "")
        )
        progress.report(0, eligible, _status_msg(state))

        bridge = asyncio.create_task(_cancellation_bridge(progress, stop))
        ticker = asyncio.create_task(_throughput_ticker(state, progress, stop))
        try:
            if strategy == "get-by-id":
                await _run_get_by_id(
                    vk, limiter, account_pk, vk_account_id_text, state, stop, progress
                )
            else:
                await _run_execute(
                    vk, limiter, account_pk, vk_account_id_text,
                    threads, per_request, state, stop, progress,
                )
            if stop.is_set():
                raise Cancelled()
        finally:
            stop.set()
            for t in (bridge, ticker):
                try:
                    await t
                except Exception:  # noqa: BLE001
                    pass

    progress.report(eligible, eligible, "done — " + _status_msg(state))
    avg = state.throughput.averages()
    summary = (
        f"fetch-data done: strategy={strategy} eligible={eligible}"
        f" fetched={state.fetched} skipped={state.skipped} missing={state.missing}"
        f" msg/s 1m/5m/15m = {avg.format()}"
    )
    progress.log(summary)
    logger.info(summary)
    return state.summary()


def _status_msg(state: "_RunState") -> str:
    return (
        f"fetched={state.fetched} skipped={state.skipped}"
        f" missing={state.missing}"
    )


def _emit_progress(
    state: "_RunState", throttle: Throttle, progress: ProgressReporter,
    *, force: bool = False,
) -> None:
    done = state.fetched + state.skipped
    total = state.eligible_total or max(done, 1)
    if force or throttle(done, total):
        progress.report(done, total, _status_msg(state))


class _RunState:
    def __init__(self) -> None:
        self.fetched = 0
        self.missing = 0
        self.skipped = 0
        self.eligible_total = 0
        self.throughput = Throughput()

    def summary(self) -> dict:
        return {
            "eligible": self.eligible_total,
            "fetched": self.fetched,
            "missing": self.missing,
            "skipped": self.skipped,
        }


async def _throughput_ticker(
    state: _RunState, progress: ProgressReporter, stop: asyncio.Event
) -> None:
    """Emit msg/s 1m/5m/15m averages every _THROUGHPUT_TICK_S seconds.

    Wakes immediately when ``stop`` is set so cancellation doesn't have
    to wait for the next 10 s deadline.
    """
    while not stop.is_set():
        try:
            await asyncio.wait_for(stop.wait(), timeout=_THROUGHPUT_TICK_S)
            return
        except asyncio.TimeoutError:
            pass
        if stop.is_set():
            return
        avg = state.throughput.averages()
        msg = (
            f"throughput msg/s 1m/5m/15m = {avg.format()}"
            f" (fetched={state.fetched} skipped={state.skipped} missing={state.missing})"
        )
        progress.log(msg)
        logger.info(msg)


async def _cancellation_bridge(
    progress: ProgressReporter, stop: asyncio.Event
) -> None:
    """Mirror the synchronous cancellation token into the asyncio Event
    every coroutine in this run watches.

    The ProgressReporter protocol only exposes ``check_cancelled()``
    (raises), so we poll it every 200 ms. As soon as the user clicks
    Stop the event flips, in-flight ``asyncio.wait_for(stop.wait(), …)``
    awaits wake instantly and the ticker / limiter / backoff sleeps
    unblock without having to ride out their full timeout.
    """
    while not stop.is_set():
        try:
            progress.check_cancelled()
        except Cancelled:
            stop.set()
            return
        try:
            await asyncio.wait_for(stop.wait(), timeout=0.2)
        except asyncio.TimeoutError:
            pass


async def _run_get_by_id(
    vk: VKApi,
    limiter: RateLimiter,
    account_pk: int,
    vk_account_id_text: str,
    state: _RunState,
    stop: asyncio.Event,
    progress: ProgressReporter,
) -> None:
    throttle = Throttle()
    for pending_ids, _chunk_last_id, chunk_skipped in _iter_pending_chunks(
        account_pk, vk_account_id_text
    ):
        if stop.is_set():
            return
        state.skipped += chunk_skipped
        _emit_progress(state, throttle, progress)
        for batch in _split(pending_ids, _BATCH_SIZE):
            if stop.is_set():
                return
            items = await _get_by_id_with_backoff(vk, limiter, batch, stop, progress)
            if stop.is_set():
                return
            got_ids = {int(m["id"]) for m in items if isinstance(m.get("id"), int)}
            state.missing += len(batch) - len(got_ids)
            if items:
                _persist_batch(account_pk, items)
                state.fetched += len(items)
                state.throughput.add(len(items))
            _emit_progress(state, throttle, progress)


async def _run_execute(
    vk: VKApi,
    limiter: RateLimiter,
    account_pk: int,
    vk_account_id_text: str,
    threads: int,
    per_request: int,
    state: _RunState,
    stop: asyncio.Event,
    progress: ProgressReporter,
) -> None:
    """Pack up to ``per_request`` ``messages.getById`` calls per ``execute``
    request and run ``threads`` of them in parallel. The rate limiter
    enforces the 3 req/s cap regardless of thread count.

    Cancellation: ``stop`` is set by the bridge task as soon as the
    user clicks Stop. Producer and workers check it at every loop
    iteration; ``queue.get`` is wrapped in ``wait_for`` so workers
    don't wedge on an empty queue while shutting down.
    """
    queue: asyncio.Queue[list[int] | None] = asyncio.Queue(maxsize=threads * 4)
    throttle = Throttle()

    async def producer() -> None:
        try:
            for pending_ids, _chunk_last_id, chunk_skipped in _iter_pending_chunks(
                account_pk, vk_account_id_text
            ):
                if stop.is_set():
                    return
                state.skipped += chunk_skipped
                _emit_progress(state, throttle, progress)
                for batch in _split(pending_ids, _BATCH_SIZE):
                    while not stop.is_set():
                        try:
                            await asyncio.wait_for(queue.put(batch), timeout=0.5)
                            break
                        except asyncio.TimeoutError:
                            continue
                    if stop.is_set():
                        return
        finally:
            # Wake every worker that's still in `queue.get()` so they can
            # observe `stop` and exit. put_nowait would race with the
            # caller's own wait_for(queue.put) — workers already poll
            # the queue with a 0.5 s timeout, so they don't need a
            # sentinel to bail out under cancellation.
            if not stop.is_set():
                for _ in range(threads):
                    try:
                        queue.put_nowait(None)
                    except asyncio.QueueFull:
                        return

    async def worker() -> None:
        while not stop.is_set():
            try:
                first = await asyncio.wait_for(queue.get(), timeout=0.5)
            except asyncio.TimeoutError:
                continue
            if first is None:
                return
            sub_batches: list[list[int]] = [first]
            while len(sub_batches) < per_request:
                try:
                    nxt = queue.get_nowait()
                except asyncio.QueueEmpty:
                    break
                if nxt is None:
                    queue.put_nowait(None)
                    break
                sub_batches.append(nxt)
            if stop.is_set():
                return
            items, missing_in_call = await _execute_get_by_id_with_backoff(
                vk, limiter, sub_batches, stop, progress
            )
            if stop.is_set():
                return
            state.missing += missing_in_call
            if items:
                _persist_batch(account_pk, items)
                state.fetched += len(items)
                state.throughput.add(len(items))
            _emit_progress(state, throttle, progress)

    workers = [asyncio.create_task(worker()) for _ in range(threads)]
    prod = asyncio.create_task(producer())
    try:
        await asyncio.gather(prod, *workers)
    except BaseException:
        # One task raised (cancellation, network, decode failure…) —
        # signal everyone else via `stop`, drain them, then re-raise
        # so the original exception reaches the orchestrator.
        stop.set()
        for t in (prod, *workers):
            try:
                await t
            except BaseException:  # noqa: BLE001
                pass
        raise


def _count_eligible_messages(vk_account_id_text: str) -> int:
    """One-shot COUNT(*) of scrapeworthy messages for the active account.

    Same predicate as :func:`_iter_pending_chunks` so the progress-bar
    denominator and the actual scan stay in lockstep. Measured at
    ~2.5 s on a 1.3M-attached-message DB; runs once at task start.
    """
    with connection() as conn:
        row = conn.execute(
            """
            SELECT COUNT(*) AS n
              FROM messages m
             WHERE m.provider='vk'
               AND m.account_id=?
               AND m.attachment_count > 0
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
            """,
            (vk_account_id_text,),
        ).fetchone()
    return int(row["n"]) if row else 0


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
    vk: VKApi, limiter: RateLimiter, ids: list[int],
    stop: asyncio.Event, progress: ProgressReporter,
) -> list[dict]:
    """One ``messages.getById`` call, paced by the shared limiter, with
    fixed-schedule backoff on VK error 6 (Too many requests). Backoff
    sleeps are wrapped against ``stop`` so a Stop click during a long
    retry wakes immediately."""
    backoffs = list(_RATE_LIMIT_BACKOFF_S) + [None]
    for i, delay in enumerate(backoffs):
        await limiter.acquire()
        if stop.is_set():
            return []
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
            await _sleep_until_stop(delay, stop)
            if stop.is_set():
                return []
            continue
        items = resp.get("items") if isinstance(resp, dict) else None
        if items is None:
            return []
        return [m for m in items if isinstance(m, dict)]
    raise RuntimeError("unreachable")


async def _sleep_until_stop(delay: float, stop: asyncio.Event) -> None:
    """Sleep up to ``delay`` seconds, returning early if ``stop`` is set."""
    try:
        await asyncio.wait_for(stop.wait(), timeout=delay)
    except asyncio.TimeoutError:
        pass


async def _execute_get_by_id_with_backoff(
    vk: VKApi,
    limiter: RateLimiter,
    batches: list[list[int]],
    stop: asyncio.Event,
    progress: ProgressReporter,
) -> tuple[list[dict], int]:
    """Pack ``batches`` (≤ 25 sub-batches of ≤ 100 ids each) into one
    ``execute`` request. Returns ``(items, missing)``.

    Sub-calls that VK reports under ``execute_errors`` with code 6
    trigger a backoff and retry of the whole execute. Other per-sub
    errors are counted toward ``missing`` along with ids that simply
    weren't returned. Every successful round-trip emits a one-line
    summary covering call count, ids in/out, per-sub-batch ok/failed
    split and elapsed wall time.
    """
    calls_in = len(batches)
    requested = sum(len(b) for b in batches)
    backoffs = list(_RATE_LIMIT_BACKOFF_S) + [None]
    for i, delay in enumerate(backoffs):
        code = _build_execute_code(batches)
        await limiter.acquire()
        if stop.is_set():
            return [], requested
        started = time.monotonic()
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
            await _sleep_until_stop(delay, stop)
            if stop.is_set():
                return [], requested
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
            await _sleep_until_stop(delay, stop)
            if stop.is_set():
                return [], requested
            continue
        items, ok_slots, failed_slots = _summarize_execute_response(response, calls_in)
        took_ms = int((time.monotonic() - started) * 1000)
        line = (
            f"responded execute(calls_in={calls_in},"
            f" message_ids_in_req_total={requested},"
            f" response_msgs_total={len(items)},"
            f" took {took_ms} msec,"
            f" batch_within execute: ok {ok_slots}, failed {failed_slots})"
        )
        progress.log(line)
        logger.info(line)
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


def _summarize_execute_response(
    response: object, calls_in: int
) -> tuple[list[dict], int, int]:
    """Flatten the ``execute`` payload and count per-slot success.

    Returns ``(items, ok_slots, failed_slots)``:

    * ``items`` — concatenated messages.getById ``items`` from every
      slot that came back as a dict with a list ``items`` field.
    * ``ok_slots`` — number of sub-batches that produced a usable
      response (any dict-shaped slot, even if its ``items`` list is
      empty — VK returns ``{"count":0,"items":[]}`` when *none* of the
      requested ids exist, and that's still a successful round-trip).
    * ``failed_slots`` — sub-batches reported as ``False`` (the slot
      sentinel for a sub-call error) plus any slots missing from a
      truncated response. Sums with ``ok_slots`` to ``calls_in``.
    """
    items: list[dict] = []
    ok = 0
    if isinstance(response, list):
        for slot in response:
            if isinstance(slot, dict):
                ok += 1
                slot_items = slot.get("items")
                if isinstance(slot_items, list):
                    for m in slot_items:
                        if isinstance(m, dict):
                            items.append(m)
    failed = max(0, calls_in - ok)
    return items, ok, failed


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
