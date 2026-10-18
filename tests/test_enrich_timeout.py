import asyncio

import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer

from vkdump.core import app_config
from vkdump.modules import enrich


class Reporter:
    def report(self, *args):
        pass

    def log(self, *args):
        pass

    def check_cancelled(self):
        pass


@pytest.mark.parametrize('timeout', [0, -1])
def test_run_rejects_nonpositive_timeout(timeout):
    with pytest.raises(ValueError, match='at least 1 second'):
        enrich.run({'timeout': timeout}, Reporter())


@pytest.mark.parametrize('params, expected', [({}, '15'), ({'timeout': 37}, '37')])
def test_run_remembers_timeout(monkeypatch, params, expected):
    saved = {}
    monkeypatch.setattr(app_config, 'set', lambda key, value: saved.update({key: value}))
    monkeypatch.setattr(enrich, '_backfill_resolutions_from_url', lambda: 0)
    monkeypatch.setattr(enrich, '_pick_rows', lambda *args, **kwargs: ([], 0))
    enrich.run(params, Reporter())
    assert saved['enrich_media.last_timeout'] == expected


@pytest.mark.parametrize('stall', ['headers', 'body'])
def test_request_timeout_fails_one_download_and_continues(monkeypatch, tmp_path, stall):
    results = {}
    monkeypatch.setattr(enrich, '_persist_result', lambda row_id, url, result: results.update({row_id: result}))

    async def scenario():
        release = asyncio.Event()
        image = b'\x89PNG\r\n\x1a\n' + b'\x00' * 128

        async def slow(request):
            response = web.StreamResponse(headers={'Content-Type': 'image/png'})
            if stall == 'body':
                await response.prepare(request)
                await response.write(image[:8])
            await release.wait()
            return response

        async def fast(request):
            return web.Response(body=image, content_type='image/png')

        app = web.Application()
        app.router.add_get('/slow', slow)
        app.router.add_get('/fast', fast)
        async with TestServer(app) as server:
            rows = [{'id': index, 'kind': 'photo', 'url': str(server.make_url(path))}
                    for index, path in enumerate(['/slow', '/fast'], 1)]
            try:
                stats = await asyncio.wait_for(enrich._download_all(
                    rows, tmp_path, concurrency=1, per_host=1,
                    timeout_s=0.05, progress=Reporter(),
                ), timeout=2)
            finally:
                release.set()
        assert stats['failed'] == 1
        assert stats['ok'] == 1
        assert stats['fail_reasons'] == {'timeout': 1}
        assert results[1]['error'] == 'timeout after 0.05s'
        assert (tmp_path / results[2]['local_path']).read_bytes() == image
        assert not list(tmp_path.rglob('*.part'))

    asyncio.run(scenario())
