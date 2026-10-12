import asyncio
import json
import re
from types import SimpleNamespace

import pytest

from vkdump.core import app_config, db
from vkdump.core.progress import Cancelled
from vkdump.core.vk_api import VKApiError, VKApiTimeout
from vkdump.modules import download_messages as download, fetch_data


class Progress:
    def __init__(self):
        self.reports = []
        self.logs = []
        self.cancelled = False

    def report(self, current, total, message=''):
        self.reports.append((current, total, message))

    def log(self, message):
        self.logs.append(message)

    def check_cancelled(self):
        if self.cancelled:
            raise Cancelled()


@pytest.fixture
def store(tmp_path, monkeypatch):
    settings = SimpleNamespace(db_path=str(tmp_path / 'test.db'), sql_debug=False)
    monkeypatch.setattr(db, 'get_settings', lambda: settings)
    db.apply_migrations()
    with db.connection() as conn:
        conn.execute("INSERT INTO accounts(provider, vk_id, vk_access_token) VALUES('vk', 42, 'test-token')")
        conn.execute("INSERT INTO accounts(provider, vk_id, vk_access_token) VALUES('vk', 43, 'other-token')")
    app_config.set('active_account_id', '42')
    monkeypatch.setattr(download, '_MAX_RPS', 100000)
    return settings


def message(mid, peer):
    return {'id': mid, 'peer_id': peer, 'from_id': 42, 'date': 10000 + mid,
            'conversation_message_id': mid, 'text': 'текст <tag>', 'out': 1,
            'attachments': [{'type': 'photo', 'photo': {'id': 123, 'sizes': [{'url': 'url'}]}}],
            'fwd_messages': [{'from_id': 10, 'text': 'forward', 'date': 12}],
            'reply_message': {'id': 7, 'text': 'reply', 'attachments': []},
            'geo': {'coordinates': {'latitude': 1.25}}, 'new_unknown_field': [False, None, {'x': 'y'}]}


class FakeVK:
    def __init__(self, histories, archived=()):
        self.histories = histories
        self.archived = set(archived)
        self.calls = []
        self.executes = []
        self.inflight = 0
        self.max_inflight = 0
        self.fail_offset = None
        self.user_id = 42

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        pass

    async def call(self, method, **params):
        self.calls.append((method, dict(params)))
        if method == 'users.get':
            return [{'id': self.user_id}]
        if method == 'messages.getConversations':
            assert params['filter'] == 'all'
            peers = list(self.histories)
            items = []
            for peer in peers[params['offset']:params['offset'] + params['count']]:
                history = self.histories[peer]
                items.append({'conversation': {'peer': {'id': peer, 'type': 'chat' if peer > 2000000000 else 'group' if peer < 0 else 'user'},
                                                'chat_settings': {'title': 'Conference'} if peer > 2000000000 else {}},
                              'last_message': history[0] if history else {}})
            return {'count': len(peers), 'items': items,
                    'profiles': [{'id': p, 'first_name': 'User', 'last_name': str(p)} for p in peers if 0 < p < 2000000000],
                    'groups': [{'id': -p, 'name': 'Bot or community'} for p in peers if p < 0]}
        if method == 'messages.getHistory':
            if params['offset'] == self.fail_offset:
                raise VKApiError(5, 'no permission')
            history = self.histories[params['peer_id']]
            anchor = params.get('start_message_id')
            skipped = next((i for i, m in enumerate(history) if m['id'] == anchor), 0) if anchor else 0
            offset = skipped + params['offset']
            return {'count': len(history), 'skipped': skipped,
                    'items': history[offset:offset + params['count']]}
        raise AssertionError(method)

    async def execute(self, code):
        params = [json.loads(s) for s in re.findall(r'API.messages.getHistory\((\{.*?\})\)', code)]
        self.executes.append(params)
        self.inflight += 1
        self.max_inflight = max(self.inflight, self.max_inflight)
        await asyncio.sleep(0)
        try:
            return [await self.call('messages.getHistory', **p) for p in params], []
        finally:
            self.inflight -= 1


def run_fake(monkeypatch, fake, **params):
    monkeypatch.setattr(download, 'VKApi', lambda token: fake)
    progress = Progress()
    return download.run(params, progress), progress


def test_complete_histories_format_concurrency_and_resume(store, monkeypatch):
    histories = {10: [message(i, 10) for i in range(1000, 549, -1)],
                 -11: [message(i, -11) for i in range(2000, 1798, -1)],
                 2000000001: [message(3000, 2000000001)], 12: []}
    fake = FakeVK(histories, archived=[-11])
    result, progress = run_fake(monkeypatch, fake, threads=2, per_request=2)
    assert result['messages'] == 654
    assert result['dialogs'] == 4
    assert fake.max_inflight == 2
    assert max(map(len, fake.executes)) == 2
    with db.connection() as conn:
        rows = conn.execute('SELECT vk_message_id, message_data, attachment_data FROM api_messages WHERE account_id=1').fetchall()
        assert len(rows) == 654
        expected = {m['id']: m for messages in histories.values() for m in messages}
        for row in rows:
            assert json.loads(row['message_data']) == expected[row['vk_message_id']]
            assert json.loads(row['attachment_data']) == expected[row['vk_message_id']]['attachments']
        dialogs = conn.execute('SELECT title, history_status FROM api_dialogs ORDER BY peer_id').fetchall()
        assert all(row['history_status'] == 'complete' for row in dialogs)
        assert dialogs[0]['title'] == 'Bot or community'
        assert dialogs[-1]['title'] == 'Conference'
        assert conn.execute('SELECT COUNT(*) FROM api_messages WHERE account_id=2').fetchone()[0] == 0
        row = conn.execute('SELECT message_count, first_message_at, last_message_at FROM api_dialogs WHERE peer_id=10').fetchone()
        assert row['message_count'] == 451
        assert row['first_message_at'] == 10550
        assert row['last_message_at'] == 11000
    assert progress.reports[-1][:2] == (654, 654)
    fake.calls.clear()
    result, _ = run_fake(monkeypatch, fake, threads=2, per_request=2)
    assert all(p['offset'] == 0 for method, p in fake.calls if method == 'messages.getHistory')
    with db.connection() as conn:
        assert conn.execute('SELECT COUNT(*) FROM api_messages').fetchone()[0] == 654
    fetch_data._persist_batch(2, [histories[10][0]])
    with db.connection() as conn:
        original = conn.execute('SELECT message_data, attachment_data FROM api_messages WHERE account_id=1 AND vk_message_id=1000').fetchone()
        via_fetch = conn.execute('SELECT message_data, attachment_data FROM api_messages WHERE account_id=2').fetchone()
        assert tuple(original) == tuple(via_fetch)


def test_conversation_pagination_uses_all_filter_only(store, monkeypatch):
    fake = FakeVK({i: [] for i in range(1, 204)}, archived=[203])
    result, _ = run_fake(monkeypatch, fake)
    assert result['dialogs'] == 203
    requests = [p for method, p in fake.calls if method == 'messages.getConversations']
    assert [(p['filter'], p['offset']) for p in requests] == [('all', 0), ('all', 200)]
    assert all(len(batch) <= 25 for batch in fake.executes)


def test_failed_run_resumes_completed_pages(store, monkeypatch):
    fake = FakeVK({10: [message(i, 10) for i in range(1000, 399, -1)]})
    fake.fail_offset = 400
    with pytest.raises(VKApiError):
        run_fake(monkeypatch, fake, threads=1, per_request=1)
    with db.connection() as conn:
        assert conn.execute('SELECT COUNT(*) FROM api_messages').fetchone()[0] == 400
        assert conn.execute('SELECT history_status FROM api_dialogs').fetchone()[0] == 'pending'
    fake.fail_offset = None
    fake.calls.clear()
    result, _ = run_fake(monkeypatch, fake, threads=1, per_request=1)
    assert result['messages'] == 601
    assert [p['offset'] for method, p in fake.calls if method == 'messages.getHistory'] == [0, 400, 600]


def test_new_anchor_rescans_history(store, monkeypatch):
    fake = FakeVK({10: [message(i, 10) for i in range(1000, 599, -1)]})
    run_fake(monkeypatch, fake)
    fake.histories[10].insert(0, message(1001, 10))
    result, _ = run_fake(monkeypatch, fake)
    assert result['messages'] == 402
    with db.connection() as conn:
        assert conn.execute('SELECT COUNT(*) FROM api_messages').fetchone()[0] == 402
        assert conn.execute('SELECT DISTINCT anchor_id FROM api_history_pages').fetchall()[0][0] == 1001


def test_account_identity_is_checked_before_discovery(store, monkeypatch):
    fake = FakeVK({})
    fake.user_id = 43
    with pytest.raises(RuntimeError, match='different account'):
        run_fake(monkeypatch, fake)
    assert [method for method, _ in fake.calls] == ['users.get']


def test_invalid_options_and_unauthorized_account(store):
    for params in ({'threads': 0}, {'per_request': 0}, {'per_request': 26}):
        with pytest.raises(ValueError):
            download.run(params, Progress())
    with pytest.raises(RuntimeError, match='Authorize'):
        download.run({'account': 999}, Progress())


def test_short_and_overlapping_history_are_not_marked_complete(store, monkeypatch):
    fake = FakeVK({10: [message(i, 10) for i in range(1000, 599, -1)]})
    original = fake.call
    async def broken(method, **params):
        response = await original(method, **params)
        if method == 'messages.getHistory' and params['offset'] == 200:
            response['items'] = fake.histories[10][:200]
        return response
    fake.call = broken
    with pytest.raises(RuntimeError, match='Overlapping'):
        run_fake(monkeypatch, fake, threads=1)
    with db.connection() as conn:
        assert conn.execute('SELECT history_status FROM api_dialogs').fetchone()[0] == 'pending'
        assert conn.execute('SELECT COUNT(*) FROM api_history_pages').fetchone()[0] == 1


def test_cancellation_preserves_pages(store, monkeypatch):
    fake = FakeVK({10: [message(i, 10) for i in range(1000, 599, -1)]})
    progress = Progress()
    report = progress.report
    def cancel(current, total, message=''):
        report(current, total, message)
        if message.startswith('Downloading messages'):
            progress.cancelled = True
    progress.report = cancel
    monkeypatch.setattr(download, 'VKApi', lambda token: fake)
    with pytest.raises(Cancelled):
        download.run({}, progress)
    with db.connection() as conn:
        assert conn.execute('SELECT COUNT(*) FROM api_messages').fetchone()[0] == 200
        assert conn.execute('SELECT history_status FROM api_dialogs').fetchone()[0] == 'pending'


class Limiter:
    async def acquire(self):
        pass


@pytest.mark.parametrize('failure', ['top-rate', 'sub-rate', 'timeout', 'hard', 'partial'])
def test_execute_retries_and_fallback(monkeypatch, failure):
    class VK:
        attempts = 0
        singles = 0
        async def execute(self, code):
            self.attempts += 1
            if self.attempts == 1:
                if failure == 'top-rate':
                    raise VKApiError(6, 'rate')
                if failure == 'timeout':
                    raise VKApiTimeout(None, 'timeout')
                if failure == 'hard':
                    raise VKApiError(13, 'too big')
                if failure == 'sub-rate':
                    return [False], [{'error_code': 6}]
                return [False], [{'error_code': 15}]
            return [{'count': 1, 'items': [message(1, 10)]}], []
        async def call(self, method, **params):
            self.singles += 1
            return {'count': 1, 'items': [message(1, 10)]}
    async def no_sleep(*args):
        pass
    monkeypatch.setattr(download, '_sleep_until_stop', no_sleep)
    vk = VK()
    response = asyncio.run(download._execute(vk, Limiter(), [download._history_params(10)], asyncio.Event(), Progress()))
    assert response[0]['items'][0] == message(1, 10)
    assert vk.singles == (1 if failure in ('hard', 'partial') else 0)
    assert vk.attempts == (1 if failure in ('hard', 'partial') else 2)


def test_detailed_request_errors_are_logged_before_failure(store, monkeypatch):
    fake = FakeVK({})
    async def denied(method, **params):
        if method == 'users.get':
            return [{'id': 42}]
        raise VKApiError(15, 'Access denied', method=method,
                         details={'error_code': 15, 'error_msg': 'Access denied',
                                  'request_params': [{'key': 'filter', 'value': params['filter']}]})
    fake.call = denied
    monkeypatch.setattr(download, 'VKApi', lambda token: fake)
    progress = Progress()
    with pytest.raises(VKApiError):
        download.run({}, progress)
    log = '\n'.join(progress.logs)
    assert 'method="messages.getConversations"' in log
    assert '"filter": "all"' in log
    assert '"offset": 0' in log
    assert 'code=15' in log and 'Access denied' in log
    assert 'test-token' not in log
    assert 'log_file=' in log
    assert 'filter=archive' not in log


def test_download_progress_is_also_persisted_to_log_file(store, monkeypatch, tmp_path):
    from loguru import logger
    path = tmp_path / 'download.log'
    sink = logger.add(path, format='{message}')
    try:
        _, progress = run_fake(monkeypatch, FakeVK({10: [message(1, 10)]}))
    finally:
        logger.remove(sink)
    text = path.read_text()
    for event in ('start |', 'phase |', 'discovery-complete |', 'complete |'):
        assert event in text
        assert any(event in line for line in progress.logs)
    for event in ('request |', 'response |', 'execute-slot |', 'batch-complete |', 'dialogs-saved |', 'history-ready |', 'page-saved |'):
        assert event not in text
        assert not any(event in line for line in progress.logs)
    assert 'test-token' not in text
    assert 'текст <tag>' not in text
