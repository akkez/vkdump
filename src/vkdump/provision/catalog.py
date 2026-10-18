from __future__ import annotations

import json
import os
import platform
import tempfile
from dataclasses import dataclass
from importlib.resources import files
from pathlib import Path
from xml.etree import ElementTree


class PreparationError(Exception):
    pass


class ApkRequired(PreparationError):
    pass


def manifest() -> dict:
    return json.loads(files(__package__).joinpath('artifacts.json').read_text(encoding='utf-8'))


def host_target(system: str | None = None, machine: str | None = None) -> str:
    system = system or platform.system()
    machine = (machine or platform.machine()).lower()
    arch = {'amd64': 'x86_64', 'aarch64': 'arm64'}.get(machine, machine)
    os_name = {'Darwin': 'macos', 'Windows': 'windows', 'Linux': 'linux'}.get(system, system.lower())
    target = f'{os_name}-{arch}'
    if target not in manifest()['platforms']:
        raise PreparationError(
            f'Android preparation is not supported on {system} {machine}. '
            'Supported: macOS Apple Silicon / Intel, Windows x64 and Linux x64. '
            'Linux ARM and Windows ARM are not supported.')
    return target


def download_directory(target: str) -> Path:
    user = str(os.getuid()) if hasattr(os, 'getuid') else 'user'
    return Path(tempfile.gettempdir()) / f'vkdump-android-{user}' / target


@dataclass(frozen=True)
class Artifact:
    id: str
    name: str
    url: str
    size: int
    algorithm: str
    checksum: str
    package: str = ''
    repository: str = ''

    @property
    def filename(self) -> str:
        return self.url.rsplit('/', 1)[-1].split('?', 1)[0]


def artifacts(target: str) -> list[Artifact]:
    return [Artifact(**item) for item in manifest()['platforms'][target]['artifacts']]


def verify_repository(data: bytes, expected: list[Artifact]) -> None:
    try:
        root = ElementTree.fromstring(data)
    except ElementTree.ParseError as exc:
        raise PreparationError('Google returned an unreadable Android download catalog. Please retry.') from exc
    for node in root.iter():
        node.tag = node.tag.rsplit('}', 1)[-1]
    for artifact in expected:
        found = False
        for package in root.findall('remotePackage'):
            if package.get('path') != artifact.package:
                continue
            for complete in package.findall('archives/archive/complete'):
                checksum = complete.find('checksum')
                if (complete.findtext('url') == artifact.filename
                        and complete.findtext('size') == str(artifact.size)
                        and checksum is not None
                        and checksum.get('type', 'sha1') == artifact.algorithm
                        and (checksum.text or '').strip().lower() == artifact.checksum):
                    found = True
        if not found:
            raise PreparationError(
                f'The pinned {artifact.name} is missing or changed in the Google catalog. '
                'No replacement version was downloaded. Update VKdump or retry later.')


def host_plan(target: str) -> dict:
    config = manifest()['platforms'][target]
    kvm = Path('/dev/kvm')
    has_kvm = kvm.exists() and os.access(kvm, os.R_OK | os.W_OK)
    return {
        'target': target, 'abi': config['abi'], 'avd_name': 'phone',
        'acceleration': config['acceleration'],
        'gpu_modes': ['software'] if target == 'linux-x86_64' and not has_kvm else ['auto', 'software'],
        'acceleration_check': 'pending', 'gpu_check': 'pending',
    }
