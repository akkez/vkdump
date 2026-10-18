from __future__ import annotations

import json
import os
import shutil
import threading
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from ..core.formatting import format_bytes
from .apk import prepare_apk
from .catalog import Artifact, artifacts, download_directory, host_plan, host_target, manifest, PreparationError, verify_repository
from .download import Downloader


@dataclass(frozen=True)
class PreparationProgress:
    overall_current: int
    overall_total: int
    file_current: int
    file_total: int
    message: str
    downloading: bool = False


@contextmanager
def _exclusive(directory):
    with (directory / '.download-lock').open('a+b') as lock:
        if os.name == 'nt':
            import msvcrt
            lock.write(b'0')
            lock.flush()
            lock.seek(0)
            acquire = lambda: msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
            release = lambda: msvcrt.locking(lock.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl
            acquire = lambda: fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            release = lambda: fcntl.flock(lock.fileno(), fcntl.LOCK_UN)
        try:
            acquire()
        except OSError as exc:
            raise PreparationError('Another VKdump window is downloading Android files. Wait for it to finish.') from exc
        try:
            yield
        finally:
            release()


def prepare(progress, cancelled: threading.Event, local_apk: Path | None = None,
            target: str | None = None, directory: Path | None = None, opener=None) -> Path:
    target = target or host_target()
    directory = directory or download_directory(target)
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    with _exclusive(directory):
        return _prepare(progress, cancelled, local_apk, target, directory, opener)


def _prepare(progress, cancelled, local_apk, target, directory, opener):
    selected = artifacts(target)
    total = sum(item.size for item in selected) + manifest()['apk']['size']
    completed = 0

    def report(current, size, message, downloading=False):
        progress(PreparationProgress(completed + current, total, current, size, message, downloading))

    client = Downloader(cancelled, report, opener,
                        transfer_progress=lambda current, size, message: report(current, size, message, True))
    report(0, 0, 'Checking Android download catalogs…')
    valid = {item.id: client.verified(directory / item.filename, item) for item in selected}
    needed = 256 * 1024 * 1024
    for item in selected:
        partial = directory / (item.filename + '.part')
        if not valid[item.id]:
            partial_size = partial.stat().st_size if partial.exists() else 0
            needed += item.size - (partial_size if partial_size < item.size else 0)
    pin = manifest()['apk']
    apk = Artifact('vk', pin['name'], '', pin['size'], pin['algorithm'], pin['checksum'])
    if not client.verified(directory / pin['filename'], apk):
        needed += pin['size']
    if shutil.disk_usage(directory).free < needed:
        raise PreparationError(f'Not enough temporary disk space. Free at least {format_bytes(needed)} and retry.')
    for repo in dict.fromkeys(item.repository for item in selected):
        client.check_cancelled()
        relevant = [item for item in selected if item.repository == repo]
        if all(valid[item.id] for item in relevant):
            continue
        verify_repository(client.metadata(repo), relevant)
    for item in selected:
        client.check_cancelled()
        client.fetch(item, directory / item.filename)
        completed += item.size
        report(0, 0, f'{item.name}: ready')
    prepare_apk(client, directory, report, local_apk)
    client.check_cancelled()
    plan = host_plan(target)
    plan.update({'downloads_verified': True, 'emulator_started': False, 'account_authorized': False})
    temporary = directory / 'preparation.json.tmp'
    temporary.write_text(json.dumps(plan, indent=2) + '\n', encoding='utf-8')
    temporary.replace(directory / 'preparation.json')
    progress(PreparationProgress(total, total, manifest()['apk']['size'], manifest()['apk']['size'], 'All files verified.'))
    return directory
