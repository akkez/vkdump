from __future__ import annotations

import json
import re
from html.parser import HTMLParser
from http.client import HTTPException
from pathlib import Path
from urllib.parse import urljoin, urlsplit

from ..core.progress import Cancelled
from .catalog import ApkRequired, Artifact, PreparationError, manifest
from .download import Downloader


class _Links(HTMLParser):
    def __init__(self, html: str):
        super().__init__()
        self.links = []
        self.feed(html)

    def handle_starttag(self, tag, attrs):
        if tag == 'a':
            self.links.append({key: value or '' for key, value in attrs})


def _https(url: str) -> str:
    if urlsplit(url).scheme != 'https' or not urlsplit(url).hostname:
        raise PreparationError('The APK source returned an invalid download address.')
    return url


def _page(client: Downloader, url: str) -> str:
    return client.metadata(_https(url)).decode('utf-8', errors='replace')


def resolve_source(source: dict, pin: dict, client: Downloader):
    kind = source['type']
    if kind == 'direct':
        yield _https(source['url'])
    elif kind == 'github-release':
        release = json.loads(client.metadata(_https(source['api_url'])))
        releases = release if isinstance(release, list) else [release]
        for entry in releases:
            for asset in entry.get('assets', []):
                if asset.get('name') == pin['filename']:
                    yield _https(asset['browser_download_url'])
    elif kind == 'rustore':
        headers = {'ruStoreVerCode': '1000', 'Content-Type': 'application/json; charset=utf-8'}
        info = json.loads(client.metadata(source['info_url'], headers=headers))['body']
        if (info.get('packageName') != pin['package']
                or str(info.get('versionCode')) != str(pin['version_code'])
                or info.get('versionName') != pin['version_name']):
            raise PreparationError(f"Required build {pin['version_code']} is not listed (found {info.get('versionCode')}).")
        payload = json.dumps({'appId': info['appId'], 'firstInstall': True}).encode()
        result = json.loads(client.metadata(source['download_url'], payload, headers))['body']
        if str(result.get('versionCode')) != str(pin['version_code']):
            raise PreparationError('The store changed the APK version during download resolution.')
        yield _https(result['apkUrl'])
    elif kind == 'apkpure':
        page_url = source['page_url']
        links = _Links(_page(client, page_url)).links
        pattern = rf'(?<![\d.]){re.escape(pin["version_name"])}(?![\d.])'
        variants = list(dict.fromkeys(
            urljoin(page_url, link['href']) for link in links
            if link.get('href') and '/download' in link['href']
            and re.search(pattern, link.get('title', '') + ' ' + link['href'])))
        for variant in variants[:4]:
            if urlsplit(variant).hostname != urlsplit(page_url).hostname:
                continue
            for link in _Links(_page(client, variant)).links:
                if str(link.get('data-dt-version_code', '')) == str(pin['version_code']):
                    url = urljoin(variant, link.get('href', ''))
                    if urlsplit(url).hostname in ('d.apkpure.com', 'download.apkpure.com'):
                        yield _https(url)
    elif kind == 'apkmirror':
        release_url = source['page_url']
        variants = list(dict.fromkeys(
            urljoin(release_url, link['href']) for link in _Links(_page(client, release_url)).links
            if link.get('href', '').split('?')[0].endswith('-android-apk-download/')))
        for variant in variants[:6]:
            if urlsplit(variant).hostname != urlsplit(release_url).hostname:
                continue
            html = _page(client, variant)
            if not re.search(rf'\b{pin["version_code"]}\b', html):
                continue
            for link in _Links(html).links:
                if 'downloadButton' not in link.get('class', '').split():
                    continue
                gate = urljoin(variant, link.get('href', ''))
                if urlsplit(gate).hostname != urlsplit(release_url).hostname:
                    continue
                for download in _Links(_page(client, gate)).links:
                    href = download.get('href', '')
                    if '/wp-content/themes/APKMirror/download.php?' in href:
                        yield _https(urljoin(gate, href))
    else:
        raise PreparationError(f'Unknown APK source type: {kind}')


def prepare_apk(client: Downloader, directory: Path, progress, local: Path | None = None) -> Path:
    pin = manifest()['apk']
    target = directory / pin['filename']
    expected = Artifact('vk', pin['name'], 'https://local.invalid/' + pin['filename'],
                        pin['size'], pin['algorithm'], pin['checksum'])
    if client.verified(target, expected):
        progress(pin['size'], pin['size'], 'VK APK: verified')
        return target
    if local is not None:
        progress(0, pin['size'], 'Verifying selected VK APK…')
        try:
            valid = local.is_file() and client.verified(local, expected)
        except OSError as exc:
            raise ApkRequired('Could not read the selected APK. Choose another file or check its permissions.') from exc
        if not valid:
            raise ApkRequired(
                f'This file does not match VK {pin["version_name"]} / {pin["version_code"]}. '
                'Choose the original APK for this build.')
        partial = target.with_suffix('.apk.part')
        with local.open('rb') as src, partial.open('wb') as dst:
            while chunk := src.read(1024 * 1024):
                client.check_cancelled()
                dst.write(chunk)
        if not client.verified(partial, expected):
            partial.unlink(missing_ok=True)
            raise ApkRequired('The selected APK changed while it was being copied. Please choose it again.')
        partial.replace(target)
        progress(pin['size'], pin['size'], 'VK APK: verified')
        return target
    diagnostics = []
    sources = list(pin['sources'])
    custom = directory / 'apk-sources.json'
    if custom.exists():
        sources.extend(json.loads(custom.read_text(encoding='utf-8')))
    for index, source in enumerate(sources):
        client.check_cancelled()
        name = source.get('name', f'Source {index + 1}')
        progress(0, pin['size'], f'Looking for VK {pin["version_name"]} in {name}…')
        try:
            found = False
            for url in resolve_source(source, pin, client):
                found = True
                candidate = Artifact('vk', f'VK APK · {name}', url,
                                     pin['size'], pin['algorithm'], pin['checksum'])
                path = client.fetch(candidate, directory / f'vk-source-{index}.apk')
                path.replace(target)
                return target
            if not found:
                diagnostics.append({'source': name, 'result': 'Pinned APK not listed'})
        except Cancelled:
            raise
        except (OSError, HTTPException, ValueError, KeyError, TypeError, PreparationError) as exc:
            diagnostics.append({'source': name, 'result': str(exc)[:500]})
        (directory / 'apk-sources-result.json').write_text(json.dumps(diagnostics, indent=2), encoding='utf-8')
    raise ApkRequired(
        'The required VK APK was not available from the download sources. '
        f'Choose VK {pin["version_name"]} / {pin["version_code"]} as an APK file; its checksum will be verified. '
        'Android downloads are saved and will be reused.')
