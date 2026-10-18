from __future__ import annotations

import hashlib
from http.client import HTTPException
import re
import ssl
import threading
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

import certifi

from ..core.progress import Cancelled
from .catalog import Artifact, PreparationError


class Downloader:
    def __init__(self, cancelled: threading.Event, progress, opener=None, transfer_progress=None):
        self.cancelled = cancelled
        self.progress = progress
        self.transfer_progress = transfer_progress or progress
        self._context = ssl.create_default_context(cafile=certifi.where())
        self._opener = opener

    def check_cancelled(self):
        if self.cancelled.is_set():
            raise Cancelled()

    def open(self, request):
        self.check_cancelled()
        if self._opener:
            return self._opener(request)
        return urlopen(request, timeout=10, context=self._context)

    def metadata(self, url: str, payload: bytes | None = None, headers: dict | None = None) -> bytes:
        request = Request(url, data=payload, headers={
            'User-Agent': 'VKdump/0.4 Android preparation', **(headers or {})})
        with self.open(request) as response:
            data = response.read(8 * 1024 * 1024 + 1)
        self.check_cancelled()
        if len(data) > 8 * 1024 * 1024:
            raise PreparationError('The download catalog is unexpectedly large. Please retry later.')
        return data

    def verified(self, path: Path, artifact: Artifact) -> bool:
        if not path.is_file() or path.stat().st_size != artifact.size:
            return False
        digest = hashlib.new(artifact.algorithm)
        with path.open('rb') as stream:
            while chunk := stream.read(1024 * 1024):
                self.check_cancelled()
                digest.update(chunk)
        return digest.hexdigest() == artifact.checksum

    def fetch(self, artifact: Artifact, destination: Path) -> Path:
        self.check_cancelled()
        if self.verified(destination, artifact):
            self.progress(artifact.size, artifact.size, f'{artifact.name}: verified')
            return destination
        destination.unlink(missing_ok=True)
        partial = destination.with_name(destination.name + '.part')
        for attempt in range(3):
            self.check_cancelled()
            if partial.exists() and partial.stat().st_size >= artifact.size:
                if self.verified(partial, artifact):
                    partial.replace(destination)
                    self.progress(artifact.size, artifact.size, f'{artifact.name}: verified')
                    return destination
                partial.unlink()
            try:
                self._transfer(artifact, partial)
                self.progress(artifact.size, artifact.size, f'Verifying {artifact.name}…')
                if not self.verified(partial, artifact):
                    partial.unlink(missing_ok=True)
                    raise PreparationError(f'{artifact.name} failed its checksum check. Please retry.')
                self.check_cancelled()
                partial.replace(destination)
                return destination
            except (OSError, URLError, HTTPException, PreparationError) as exc:
                if isinstance(exc, HTTPError) and exc.code == 416:
                    partial.unlink(missing_ok=True)
                if attempt == 2:
                    raise PreparationError(
                        f'Could not download {artifact.name}. {exc}. '
                        'Check your connection and retry; partial downloads will resume.') from exc
                self.progress(partial.stat().st_size if partial.exists() else 0, artifact.size,
                              f'Retrying {artifact.name} ({attempt + 2}/3)…')
                if self.cancelled.wait(2 ** attempt):
                    raise Cancelled()
        raise AssertionError('unreachable')

    def _transfer(self, artifact: Artifact, partial: Path):
        offset = partial.stat().st_size if partial.exists() else 0
        headers = {'Accept-Encoding': 'identity', 'User-Agent': 'VKdump/0.4 Android preparation'}
        if offset:
            headers['Range'] = f'bytes={offset}-'
        with self.open(Request(artifact.url, headers=headers)) as response:
            status = response.status
            if status == 206:
                match = re.fullmatch(r'bytes (\d+)-(\d+)/(\d+)', response.headers.get('Content-Range', ''))
                if (not match or int(match[1]) != offset or int(match[3]) != artifact.size
                        or int(match[2]) < offset or int(match[2]) >= artifact.size):
                    partial.unlink(missing_ok=True)
                    raise PreparationError(f'{artifact.name}: invalid resume response')
            elif status == 200:
                offset = 0
            else:
                raise PreparationError(f'{artifact.name}: unexpected HTTP status {status}')
            self.transfer_progress(offset, artifact.size, artifact.name)
            with partial.open('ab' if offset else 'wb') as stream:
                while True:
                    self.check_cancelled()
                    chunk = response.read(256 * 1024)
                    if not chunk:
                        break
                    if offset + len(chunk) > artifact.size:
                        stream.close()
                        partial.unlink(missing_ok=True)
                        raise PreparationError(f'{artifact.name}: unexpected file size')
                    stream.write(chunk)
                    offset += len(chunk)
                    self.transfer_progress(offset, artifact.size, artifact.name)
        if offset != artifact.size:
            raise PreparationError(f'{artifact.name}: interrupted download')
