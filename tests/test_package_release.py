import hashlib
import sqlite3
import sys
import tarfile
import zipfile
from pathlib import Path
from contextlib import closing
from types import SimpleNamespace

import pytest

from scripts import package_release, update_cask


@pytest.mark.parametrize('platform_name',['macos-arm64','macos-x86_64','windows-x86_64','linux-x86_64'])
def test_all_release_archives_have_one_versioned_root_and_validate_extraction(tmp_path,monkeypatch,platform_name):
    if platform_name.startswith('macos-') and sys.platform!='darwin':
        pytest.skip('macOS archives use the native ditto utility')
    dist=tmp_path/'dist'
    source=dist/('VKdump.app/Contents/MacOS' if platform_name.startswith('macos-') else 'vkdump')
    source.mkdir(parents=True)
    suffix='.exe' if platform_name.startswith('windows-') else ''
    for name in ('vkdump','vkdump-cli'):
        binary=source/(name+suffix)
        binary.write_bytes(name.encode())
        binary.chmod(0o755)
    checked=[]
    def check(directory,target):
        assert directory.name=='vkdump-v9.8.7'
        assert target==platform_name
        binaries=directory/'VKdump.app/Contents/MacOS' if target.startswith('macos-') else directory
        assert (binaries/('vkdump-cli'+suffix)).read_bytes()==b'vkdump-cli'
        checked.append(directory)
    monkeypatch.setattr(package_release,'check_bundle',check)
    archive=package_release.package_bundle(platform_name,'v9.8.7',dist,tmp_path/'release')
    if archive.suffix=='.zip':
        with zipfile.ZipFile(archive) as zipped:
            members=zipped.namelist()
    else:
        with tarfile.open(archive) as tar:
            members=tar.getnames()
            assert tar.getmember('vkdump-v9.8.7/vkdump-cli').mode & 0o111
    assert {name.split('/')[0] for name in members}=={'vkdump-v9.8.7'}
    assert len(checked)==1


def test_release_tag_defaults_only_for_build_only_runs(monkeypatch,tmp_path):
    assert package_release.version_tag('v0.3')=='v0.3'
    assert package_release.version_tag('0.3')=='v0.3'
    (tmp_path/'pyproject.toml').write_text('[project]\nversion="9.8.0"\n')
    monkeypatch.setattr(package_release,'ROOT',tmp_path)
    assert package_release.version_tag()=='v9.8.0'
    for raw in ('../escape','v0.3/other','master','v0.3;echo'):
        with pytest.raises(ValueError):
            package_release.version_tag(raw)
    called=[]
    monkeypatch.setenv('RELEASE_TAG','v9.8.7')
    monkeypatch.setattr(sys,'argv',['package_release.py','--platform','linux-x86_64'])
    monkeypatch.setattr(package_release,'package_bundle',lambda platform,tag:called.append((platform,tag)))
    package_release.main()
    assert called==[('linux-x86_64','v9.8.7')]


def test_generated_cask_installs_app_from_versioned_directory(tmp_path,monkeypatch):
    artifacts=tmp_path/'release';artifacts.mkdir()
    for arch in ('arm64','x86_64'):
        (artifacts/f'vkdump-macos-{arch}.zip').write_bytes(arch.encode())
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(sys,'argv',['update_cask.py','v9.8.7','--artifacts',str(artifacts)])
    update_cask.main()
    cask=(tmp_path/'Casks'/'vkdump.rb').read_text()
    assert 'app "vkdump-v#{version}/VKdump.app"' in cask
    assert 'version "9.8.7"' in cask
    assert hashlib.sha256(b'arm64').hexdigest() in cask


def test_bundle_checks_share_cli_test_database_and_isolate_gui(tmp_path,monkeypatch):
    root=tmp_path/'repo'
    migrations=root/'migrations';migrations.mkdir(parents=True)
    (migrations/'001_test.sql').write_text('')
    monkeypatch.setattr(package_release,'ROOT',root)
    monkeypatch.setenv('DB_PATH',str(tmp_path/'working.db'))
    calls=[]
    def run(command,**kwargs):
        database=Path(kwargs['env']['DB_PATH'])
        assert database.parent==kwargs['cwd']
        assert database!=tmp_path/'working.db'
        calls.append((command,database))
        if len(command)==1:
            assert not database.exists()
            return SimpleNamespace(returncode=2,stdout='download-messages')
        with closing(sqlite3.connect(database)) as conn:
            conn.execute('CREATE TABLE _migrations(name TEXT)')
            conn.execute("INSERT INTO _migrations VALUES('001_test.sql')")
            conn.commit()
        return SimpleNamespace(returncode=0)
    monkeypatch.setattr(package_release.subprocess,'run',run)
    package_release.check_bundle(tmp_path/'bundle','linux-x86_64')
    assert len(calls)==3
    assert calls[0][1]==calls[1][1]
    assert calls[2][1]!=calls[1][1]
    assert not any(database.exists() for _,database in calls)
    assert not (tmp_path/'working.db').exists()
