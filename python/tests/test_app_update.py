import hashlib
import io
import zipfile

import pytest

import app_update


def make_release(extra_name=None):
    payload = io.BytesIO()
    with zipfile.ZipFile(payload, "w") as archive:
        for name in app_update.APP_FILES:
            archive.writestr(name, 'APP_VERSION = "0.1.1"\n')
        if extra_name:
            archive.writestr(extra_name, "")
    data = payload.getvalue()
    url = f"https://github.com/{app_update.REPOSITORY}/releases/download/v0.1.1/{app_update.ASSET_NAME}"
    return app_update.Release("v0.1.1", url, hashlib.sha256(data).hexdigest(), len(data)), data


@pytest.fixture
def installation(tmp_path):
    for name in app_update.APP_FILES:
        (tmp_path / name).write_text("# installed\n", encoding="utf-8")
    (tmp_path / "config.json").write_text("settings", encoding="utf-8")
    return tmp_path


def test_latest_release_requires_verified_asset(monkeypatch):
    import json

    release, _ = make_release()
    asset = {"name": app_update.ASSET_NAME, "size": release.size,
             "digest": "sha256:" + release.digest, "browser_download_url": release.url}
    metadata = {"tag_name": release.tag, "assets": [asset]}
    native = {"tag_name": "native-v0.1.2", "assets": []}
    monkeypatch.setattr(app_update, "_get", lambda *_: json.dumps([native, {"tag_name": "vnext"}, metadata]).encode())
    assert app_update.latest_release() == release
    metadata["prerelease"] = True
    assert app_update.latest_release() is None
    metadata["prerelease"] = False
    asset["browser_download_url"] = "https://example.com/update.zip"
    with pytest.raises(ValueError, match="SHA-256"):
        app_update.latest_release()


def test_package_rejects_traversal_and_modified_download():
    release, data = make_release("../config.json")
    with pytest.raises(ValueError, match="文件列表"):
        app_update._read_package(data, release)
    release, data = make_release()
    with pytest.raises(ValueError, match="SHA-256"):
        app_update._read_package(data[:-1] + bytes([data[-1] ^ 1]), release)


def test_install_replaces_only_program_files_and_keeps_backup(installation, monkeypatch):
    release, data = make_release()
    monkeypatch.setattr(app_update, "_get", lambda *_: data)
    work = app_update.prepare_update(release, installation)
    restarts = []
    app_update.install_update(work, installation, lambda: restarts.append(True))
    assert restarts == [True]
    for name in app_update.APP_FILES:
        assert "0.1.1" in (installation / name).read_text(encoding="utf-8")
        assert "installed" in (work / "backup" / name).read_text(encoding="utf-8")
    assert (installation / "config.json").read_text(encoding="utf-8") == "settings"


def test_install_rolls_back_on_partial_replacement_failure(installation, monkeypatch):
    release, data = make_release()
    monkeypatch.setattr(app_update, "_get", lambda *_: data)
    work = app_update.prepare_update(release, installation)
    real_replace = app_update.os.replace

    def fail_second(source, dest):
        if dest.name == app_update.APP_FILES[1]:
            raise OSError("file locked")
        real_replace(source, dest)

    monkeypatch.setattr(app_update.os, "replace", fail_second)
    with pytest.raises(OSError, match="file locked"):
        app_update.install_update(work, installation, lambda: pytest.fail("unexpected restart"))
    for name in app_update.APP_FILES:
        assert "installed" in (installation / name).read_text(encoding="utf-8")


def test_install_preserves_edits_made_after_download(installation, monkeypatch):
    release, data = make_release()
    monkeypatch.setattr(app_update, "_get", lambda *_: data)
    work = app_update.prepare_update(release, installation)
    edited = installation / app_update.APP_FILES[-1]
    edited.write_text("# edited\n", encoding="utf-8")
    with pytest.raises(ValueError, match="已改动"):
        app_update.install_update(work, installation, lambda: pytest.fail("unexpected restart"))
    assert "edited" in edited.read_text(encoding="utf-8")
    assert "installed" in (installation / app_update.APP_FILES[0]).read_text(encoding="utf-8")


@pytest.mark.parametrize("tag", ["v0.1.1", "0.1.1"])
def test_interrupted_install_recovers_before_next_launch(installation, monkeypatch, tag):
    release, data = make_release()
    release = release._replace(tag=tag)
    monkeypatch.setattr(app_update, "_get", lambda *_: data)
    work = app_update.prepare_update(release, installation)
    real_replace = app_update.os.replace

    def interrupt(source, dest):
        if dest.name == app_update.APP_FILES[1]:
            raise KeyboardInterrupt
        real_replace(source, dest)

    with monkeypatch.context() as patch:
        patch.setattr(app_update.os, "replace", interrupt)
        with pytest.raises(KeyboardInterrupt):
            app_update.install_update(work, installation, lambda: pytest.fail("unexpected restart"))
    assert "0.1.1" in (installation / app_update.APP_FILES[0]).read_text(encoding="utf-8")
    app_update.recover_updates(installation)
    assert not (work / "in-progress.json").exists()
    for name in app_update.APP_FILES:
        assert "installed" in (installation / name).read_text(encoding="utf-8")
