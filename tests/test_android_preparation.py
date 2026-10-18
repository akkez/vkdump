import hashlib
import importlib
import io
import json
import threading
from pathlib import Path
from types import SimpleNamespace
from urllib.error import URLError

import pytest

from vkdump.core.progress import Cancelled
from vkdump.provision import apk, catalog
from vkdump.provision.catalog import Artifact, ApkRequired, PreparationError
from vkdump.provision.download import Downloader

service = importlib.import_module('vkdump.provision.prepare')


class Event(threading.Event):
    def wait(self, timeout=None):
        return self.is_set()


class Response(io.BytesIO):
    def __init__(self, data, status=200, headers=None):
        super().__init__(data)
        self.status = status
        self.headers = headers or {}


def artifact(data=b'original', url='https://example.test/file.zip', name='Test file'):
    return Artifact('test', name, url, len(data), 'sha256', hashlib.sha256(data).hexdigest())


@pytest.mark.parametrize('system,machine,target', [
    ('Darwin', 'arm64', 'macos-arm64'), ('Darwin', 'x86_64', 'macos-x86_64'),
    ('Windows', 'AMD64', 'windows-x86_64'), ('Linux', 'x86_64', 'linux-x86_64'),
])
def test_platform_catalog(system, machine, target):
    assert catalog.host_target(system, machine) == target
    files = catalog.artifacts(target)
    assert len(files) == 4
    assert sum(a.size for a in files) > 2_000_000_000
    assert all(a.url.startswith('https://dl.google.com/android/repository/') for a in files)
    assert all(len(a.checksum) == 40 for a in files)
    tools = next(a for a in files if a.id == 'tools')
    assert tools.filename.startswith('commandlinetools-')
    image = next(a for a in files if a.id == 'image')
    assert ('arm64-v8a' in image.filename) == (target == 'macos-arm64')


@pytest.mark.parametrize('system,machine', [('Windows', 'ARM64'), ('Linux', 'aarch64'), ('Linux', 'i686')])
def test_unsupported_platform_fails_before_download(system, machine):
    with pytest.raises(PreparationError, match='not supported'):
        catalog.host_target(system, machine)


def test_repository_pin_requires_matching_package_url_size_and_hash():
    item = artifact()
    item = Artifact(**{**item.__dict__, 'package': 'emulator'})
    xml = (f'<repo><remotePackage path="emulator"><archives><archive><complete>'
           f'<size>{item.size}</size><checksum type="sha256">{item.checksum}</checksum>'
           f'<url>file.zip</url></complete></archive></archives></remotePackage></repo>').encode()
    catalog.verify_repository(xml, [item])
    for changed in (xml.replace(b'file.zip', b'other.zip'), xml.replace(item.checksum.encode(), b'bad'),
                    xml.replace(b'path="emulator"', b'path="tools"')):
        with pytest.raises(PreparationError, match='missing or changed'):
            catalog.verify_repository(changed, [item])


@pytest.mark.parametrize('resumes', [True, False])
def test_download_resume_and_server_ignoring_range(tmp_path, resumes):
    expected = artifact()
    dest = tmp_path / 'file.zip'
    dest.with_suffix('.zip.part').write_bytes(b'orig')
    def open_request(request):
        assert request.headers['Range'] == 'bytes=4-'
        return Response(b'inal', 206, {'Content-Range': 'bytes 4-7/8'}) if resumes else Response(b'original')
    Downloader(Event(), lambda *args: None, open_request).fetch(expected, dest)
    assert dest.read_bytes() == b'original'
    assert not dest.with_suffix('.zip.part').exists()


def test_invalid_range_is_discarded_before_retry(tmp_path):
    dest = tmp_path / 'file.zip'
    partial = dest.with_suffix('.zip.part')
    partial.write_bytes(b'orig')
    calls = []
    def open_request(request):
        calls.append(request.headers)
        if len(calls) == 1:
            return Response(b'inal', 206, {'Content-Range': 'bytes 3-6/8'})
        assert 'Range' not in request.headers
        return Response(b'original')
    Downloader(Event(), lambda *args: None, open_request).fetch(artifact(), dest)
    assert dest.read_bytes() == b'original'
    assert len(calls) == 2


def test_checksum_mismatch_never_publishes_file(tmp_path):
    dest = tmp_path / 'file.zip'
    with pytest.raises(PreparationError, match='checksum'):
        Downloader(Event(), lambda *args: None, lambda _: Response(b'tampered')).fetch(artifact(), dest)
    assert not dest.exists()
    assert not dest.with_suffix('.zip.part').exists()


def test_cancel_preserves_partial_and_resume(tmp_path):
    data = b'x' * (512 * 1024)
    stop = Event()
    dest = tmp_path / 'large.zip'
    def progress(current, total, message):
        if current:
            stop.set()
    client = Downloader(stop, progress, lambda _: Response(data))
    with pytest.raises(Cancelled):
        client.fetch(artifact(data), dest)
    partial = dest.with_suffix('.zip.part')
    assert partial.stat().st_size == 256 * 1024
    assert not dest.exists()
    stop.clear()
    def resume(request):
        return Response(data[256 * 1024:], 206, {'Content-Range': f'bytes {256 * 1024}-{len(data)-1}/{len(data)}'})
    Downloader(stop, lambda *args: None, resume).fetch(artifact(data), dest)
    assert dest.read_bytes() == data


def test_verified_cache_never_requests_network(tmp_path):
    dest = tmp_path / 'file.zip'
    dest.write_bytes(b'original')
    def offline(_):
        raise AssertionError('Unexpected network request')
    Downloader(Event(), lambda *args: None, offline).fetch(artifact(), dest)


@pytest.fixture
def tiny_catalog(monkeypatch):
    data = catalog.manifest()
    selected = data['platforms']['macos-arm64']['artifacts']
    blobs = {}
    for index, item in enumerate(selected):
        body = f'component {index}'.encode()
        item.update(size=len(body), algorithm='sha256', checksum=hashlib.sha256(body).hexdigest())
        blobs[item['url']] = body
    body = b'original'
    data['apk'].update(size=len(body), checksum=hashlib.sha256(body).hexdigest())
    for module in (catalog, apk, service):
        monkeypatch.setattr(module, 'manifest', lambda: data)
    return data, blobs


def test_sources_fail_over_from_version_mismatch_and_bad_checksum(tmp_path, tiny_catalog):
    data, _ = tiny_catalog
    data['apk']['sources'] = [
        {'name': 'RuStore', 'type': 'rustore', 'info_url': 'https://example.test/info', 'download_url': ''},
        {'name': 'Bad mirror', 'type': 'direct', 'url': 'https://example.test/bad'},
        {'name': 'GitHub', 'type': 'github-release', 'api_url': 'https://api.github.com/repos/test/apks/releases/tags/pinned'},
    ]
    requested = []
    def opener(request):
        url = request.full_url
        requested.append(url)
        if url.endswith('/info'):
            return Response(json.dumps({'body': {'packageName': 'com.vkontakte.android', 'versionCode': 59199,
                                                  'versionName': '8.199'}}).encode())
        if url.endswith('/bad'):
            return Response(b'tampered')
        if 'api.github.com' in url:
            return Response(json.dumps({'assets': [{'name': data['apk']['filename'],
                                                    'browser_download_url': 'https://example.test/good'}]}).encode())
        return Response(b'original')
    client = Downloader(Event(), lambda *args: None, opener)
    result = apk.prepare_apk(client, tmp_path, lambda *args: None)
    assert result.read_bytes() == b'original'
    assert 'https://example.test/good' in requested
    assert requested.count('https://example.test/bad') == 3
    assert '59199' in (tmp_path / 'apk-sources-result.json').read_text()


def test_missing_sources_offer_local_apk_and_reject_bad_file(tmp_path, tiny_catalog):
    data, _ = tiny_catalog
    data['apk']['sources'] = []
    client = Downloader(Event(), lambda *args: None)
    with pytest.raises(ApkRequired, match='not available'):
        apk.prepare_apk(client, tmp_path, lambda *args: None)
    local = tmp_path / 'user.apk'
    local.write_bytes(b'tampered')
    with pytest.raises(ApkRequired, match='does not match'):
        apk.prepare_apk(client, tmp_path, lambda *args: None, local)
    local.write_bytes(b'original')
    assert apk.prepare_apk(client, tmp_path, lambda *args: None, local).read_bytes() == b'original'
    assert local.read_bytes() == b'original'


def test_apkpure_and_apkmirror_resolve_exact_build():
    pin = catalog.manifest()['apk']
    pages = {
        'https://apkpure.com/vk/versions': '<a title="Download VK v8.199 for android" href="/vk/download/8.199">VK</a>',
        'https://apkpure.com/vk/download/8.199': '<a data-dt-version_code="59196" href="https://d.apkpure.com/b/APK/pinned">Download</a>',
        'https://www.apkmirror.com/release/': '<a href="/vk-android-apk-download/">APK</a>',
        'https://www.apkmirror.com/vk-android-apk-download/': '59196 <a class="downloadButton" href="/gate">Download</a>',
        'https://www.apkmirror.com/gate': '<a href="/wp-content/themes/APKMirror/download.php?id=1">here</a>',
    }
    client = Downloader(Event(), lambda *args: None, lambda r: Response(pages[r.full_url].encode()))
    assert list(apk.resolve_source({'type': 'apkpure', 'page_url': 'https://apkpure.com/vk/versions'}, pin, client)) == ['https://d.apkpure.com/b/APK/pinned']
    assert list(apk.resolve_source({'type': 'apkmirror', 'page_url': 'https://www.apkmirror.com/release/'}, pin, client)) == ['https://www.apkmirror.com/wp-content/themes/APKMirror/download.php?id=1']


def test_complete_download_is_offline_reusable_and_does_not_authorize(tmp_path, tiny_catalog):
    data, blobs = tiny_catalog
    data['apk']['sources'] = [{'name': 'Storage', 'type': 'direct', 'url': 'https://example.test/vk.apk'}]
    blobs['https://example.test/vk.apk'] = b'original'
    def opener(request):
        if request.full_url in blobs:
            return Response(blobs[request.full_url])
        relevant = [a for a in data['platforms']['macos-arm64']['artifacts'] if a['repository'] == request.full_url]
        xml = '<repo>'
        for a in relevant:
            xml += (f'<remotePackage path="{a["package"]}"><archives><archive><complete>'
                    f'<url>{a["url"].rsplit("/", 1)[-1]}</url><size>{a["size"]}</size>'
                    f'<checksum type="sha256">{a["checksum"]}</checksum></complete></archive></archives></remotePackage>')
        return Response((xml + '</repo>').encode())
    progress = []
    directory = tmp_path / 'downloads'
    assert service.prepare(progress.append, Event(), target='macos-arm64', directory=directory, opener=opener) == directory
    plan = json.loads((directory / 'preparation.json').read_text())
    assert plan['gpu_modes'] == ['auto', 'software']
    assert plan['emulator_started'] is False
    assert plan['account_authorized'] is False
    assert progress[-1].overall_current == progress[-1].overall_total
    assert any(event.downloading for event in progress)
    assert not progress[-1].downloading
    def offline(request):
        raise AssertionError('Unexpected network request')
    service.prepare(progress.append, Event(), target='macos-arm64', directory=directory, opener=offline)
    assert {p.parent for p in tmp_path.rglob('*') if p.is_file()} == {directory}


def test_no_disk_space_stops_before_network(tmp_path, tiny_catalog, monkeypatch):
    monkeypatch.setattr(service.shutil, 'disk_usage', lambda _: SimpleNamespace(free=0))
    def opener(_):
        raise AssertionError('Unexpected network request')
    with pytest.raises(PreparationError, match='disk space'):
        service.prepare(lambda _: None, Event(), target='macos-arm64', directory=tmp_path, opener=opener)


def test_download_directory_has_exclusive_writer(tmp_path):
    with service._exclusive(tmp_path):
        with pytest.raises(PreparationError, match='Another VKdump'):
            with service._exclusive(tmp_path):
                pytest.fail('Acquired a second write lock')


def test_personal_storage_can_be_added_without_rebuilding(tmp_path, tiny_catalog):
    data, _ = tiny_catalog
    data['apk']['sources'] = []
    (tmp_path / 'apk-sources.json').write_text(json.dumps([
        {'name': 'Personal storage', 'type': 'direct', 'url': 'https://raw.githubusercontent.com/test/storage/pin/vk.apk'}]))
    client = Downloader(Event(), lambda *args: None, lambda _: Response(b'original'))
    assert apk.prepare_apk(client, tmp_path, lambda *args: None).read_bytes() == b'original'


def test_primary_github_apk_download_does_not_contact_other_sources(tmp_path, tiny_catalog):
    data, _ = tiny_catalog
    requested = []
    primary = data['apk']['sources'][0]['url']
    def opener(request):
        requested.append(request.full_url)
        assert request.full_url == primary
        return Response(b'original')
    client = Downloader(Event(), lambda *args: None, opener)
    path = apk.prepare_apk(client, tmp_path, lambda *args: None)
    assert path.name == data['apk']['filename']
    assert path.read_bytes() == b'original'
    assert requested == [primary]
