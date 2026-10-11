from pathlib import Path

from vkdump.core import db


def test_installed_macos_app_data_is_in_user_home(monkeypatch, tmp_path):
    monkeypatch.setattr(db.sys, "frozen", True, raising=False)
    monkeypatch.setattr(db.sys, "executable", str(tmp_path / "VKdump.app" / "Contents" / "MacOS" / "vkdump-gui"))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    assert db.app_dir() == tmp_path / "Library" / "Application Support" / "vkdump"


def test_portable_data_is_next_to_executable(monkeypatch, tmp_path):
    monkeypatch.setattr(db.sys, "frozen", True, raising=False)
    monkeypatch.setattr(db.sys, "executable", str(tmp_path / "vkdump-gui"))
    assert db.app_dir() == tmp_path


def test_frozen_resources_use_bundle_directory(monkeypatch, tmp_path):
    monkeypatch.setattr(db.sys, "frozen", True, raising=False)
    monkeypatch.setattr(db.sys, "_MEIPASS", str(tmp_path / "_internal"), raising=False)
    assert db.resource_dir() == tmp_path / "_internal"
