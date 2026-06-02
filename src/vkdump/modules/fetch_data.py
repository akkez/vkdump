"""Module: fetch full VK data for the active account.

Currently does two things:

1. Calls ``users.get`` once as a token sanity-check and to surface the
   authorized identity into the run log.
2. Walks the local ``messages`` table for every row with
   ``has_forwards=1`` and pulls the full VK payload via
   ``messages.getById`` (batches of 100, ``extended=1``) into the
   ``api_messages`` table. Idempotent — already-cached ids are skipped
   transparently and *still count toward the progress bar* so a
   re-run starts at the % we left off at, not at 0.

Reads `messages` with keyset pagination over `messages.id` (chunk =
_SCAN_CHUNK), never materialising the whole id list. Progress
denominator is the active account's MAX(messages.id) — a single index
probe, cheap on multi-million-row tables.

Rate limit: 3 req/s pacing + fixed-schedule backoff (2,3,5,7,10,15 s)
on VK error 6 (Too many requests).
"""
from __future__ import annotations

import asyncio
import json
from typing import Iterator

from loguru import logger

from ..core import app_config
from ..core.db import connection
from ..core.progress import ProgressReporter, Throttle
from ..core.vk_api import VKApi, VKApiError


_ACTIVE_KEY = "active_account_id"
_BATCH_SIZE = 100            # VK hard limit for messages.getById
_SCAN_CHUNK = 5000           # keyset chunk size when scanning messages
_PACE_S = 0.34               # ~3 req/s, VK user-token cap
_RATE_LIMITED_CODE = 6       # VK "Too many requests per second"
_RATE_LIMIT_BACKOFF_S = (2, 3, 5, 7, 10, 15)


def run(params: dict, progress: ProgressReporter) -> dict:
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

    return asyncio.run(_run_async(account_pk, active_vk_id, token, progress))


async def _run_async(
    account_pk: int,
    vk_account_id_text: str,
    token: str,
    progress: ProgressReporter,
) -> dict:
    fetched = 0
    missing = 0
    skipped = 0
    last_id = 0
    throttle = Throttle()

    async with VKApi(token) as vk:
        # 1. Smoke-test the token; logs who we are without a message box.
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

        # 2. Walk messages by keyset; progress denominator = MAX(messages.id)
        # restricted to has_forwards=1 so the bar reflects only relevant rows.
        max_id = _max_message_id(vk_account_id_text)
        if max_id == 0:
            progress.log("No messages in DB for the active account.")
            return _summary(fetched, missing, skipped, max_id)
        progress.report(0, max_id, "scanning…")

        for pending_ids, chunk_last_id, chunk_skipped in _iter_pending_chunks(
            account_pk, vk_account_id_text
        ):
            progress.check_cancelled()
            skipped += chunk_skipped
            last_id = chunk_last_id

            # Update the bar after every keyset chunk (whether or not we
            # actually issued an API batch for it) — otherwise long runs
            # of already-cached rows look like the task is frozen.
            progress.report(
                last_id, max_id,
                f"fetched={fetched} skipped={skipped} missing={missing}",
            )

            for batch in _split(pending_ids, _BATCH_SIZE):
                progress.check_cancelled()
                items = await _get_by_id_with_backoff(vk, batch, progress)
                got_ids = {int(m["id"]) for m in items if isinstance(m.get("id"), int)}
                missing += len(batch) - len(got_ids)
                if items:
                    _persist_batch(account_pk, items)
                    fetched += len(items)
                if throttle(last_id, max_id):
                    progress.report(
                        last_id, max_id,
                        f"fetched={fetched} skipped={skipped} missing={missing}",
                    )
                await asyncio.sleep(_PACE_S)

    progress.report(
        max_id, max_id,
        f"done — fetched={fetched} skipped={skipped} missing={missing}",
    )
    summary = (
        f"fetch-data done: fetched={fetched} skipped={skipped}"
        f" missing={missing} scanned_up_to_id={last_id} max_id={max_id}"
    )
    progress.log(summary)
    logger.info(summary)
    return _summary(fetched, missing, skipped, max_id)


def _summary(fetched: int, missing: int, skipped: int, max_id: int) -> dict:
    return {
        "fetched": fetched,
        "missing": missing,
        "skipped": skipped,
        "max_id": max_id,
    }


def _max_message_id(vk_account_id_text: str) -> int:
    with connection() as conn:
        row = conn.execute(
            "SELECT COALESCE(MAX(id), 0) AS m FROM messages"
            " WHERE provider='vk' AND account_id=? AND has_forwards=1",
            (vk_account_id_text,),
        ).fetchone()
    return int(row["m"]) if row else 0


def _iter_pending_chunks(
    account_pk: int, vk_account_id_text: str
) -> Iterator[tuple[list[int], int, int]]:
    """Yield ``(pending_vk_ids, chunk_last_id, chunk_skipped)`` for each
    keyset slice of size up to ``_SCAN_CHUNK`` over the messages with
    forwards. Two-stage scan to keep both queries index-only:

      1. Pull (id, vk_message_id) from `messages` filtered by
         has_forwards=1 — covered by partial index
         `idx_messages_with_forwards`.
      2. Pull existing vk_message_ids in this slice from `api_messages`
         via the UNIQUE(account_id, vk_message_id) index, set-diff to
         get pending. Skipped count is `len(slice) - len(pending)`.

    Avoids a wide LEFT JOIN scan that was burning ~700ms per chunk on
    real-world tables.
    """
    last_id = 0
    while True:
        with connection() as conn:
            rows = conn.execute(
                "SELECT m.id, m.vk_message_id FROM messages m"
                " WHERE m.provider='vk'"
                "   AND m.account_id=?"
                "   AND m.has_forwards=1"
                "   AND m.id > ?"
                " ORDER BY m.id ASC LIMIT ?",
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
    vk: VKApi, ids: list[int], progress: ProgressReporter
) -> list[dict]:
    """Call messages.getById, retrying on VK error 6 with the fixed
    backoff schedule from _RATE_LIMIT_BACKOFF_S. After the last delay
    is exhausted the final failure propagates as VKApiError."""
    backoffs = list(_RATE_LIMIT_BACKOFF_S) + [None]
    for i, delay in enumerate(backoffs):
        try:
            resp = await vk.call(
                "messages.getById",
                message_ids=ids,
                extended=True,
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
