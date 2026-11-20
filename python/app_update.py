"""从固定 GitHub 仓库更新程序文件，独立于托盘界面运行。"""

import ast
import hashlib
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path
from typing import NamedTuple
from urllib.error import HTTPError
from urllib.request import Request, urlopen

APP_VERSION = "0.1.0"
REPOSITORY = "XiaoHang9527/peripheral-battery-tray"
ASSET_NAME = "peripheral-battery-tray-update.zip"
APP_FILES = ("battery_protocols.py", "hid_transport.py", "vendor_battery.py",
             "tray_ui.py", "app_update.py", "peripheral_battery_tray.py")
MAX_ARCHIVE = 2 * 1024 * 1024
MAX_CONTENTS = 4 * 1024 * 1024


class Release(NamedTuple):
    tag: str
    url: str
    digest: str
    size: int


def version_number(tag):
    if not isinstance(tag, str) or not re.fullmatch(r"v?(?:0|[1-9]\d{0,4})(?:\.(?:0|[1-9]\d{0,4})){2}", tag, re.ASCII):
        raise ValueError("版本号应为 X.Y.Z")
    return tuple(map(int, tag.removeprefix("v").split(".")))


def _get(url, limit):
    request = Request(url, headers={"User-Agent": f"PeripheralBatteryTray/{APP_VERSION}"})
    with urlopen(request, timeout=12) as response:
        content = response.read(limit + 1)
    if len(content) > limit:
        raise ValueError("下载超过大小限制")
    return content


def latest_release():
    try:
        releases = json.loads(_get(f"https://api.github.com/repos/{REPOSITORY}/releases?per_page=30", MAX_ARCHIVE))
    except HTTPError as exc:
        if exc.code == 404:
            return None
        raise
    newest = version_number(APP_VERSION)
    data = None
    for release in releases:
        tag = release.get("tag_name")
        if not isinstance(tag, str) or not tag.startswith("v") or release.get("draft") or release.get("prerelease"):
            continue
        try:
            number = version_number(tag)
        except ValueError:
            continue
        if number > newest:
            newest, data = number, release
    if data is None:
        return None
    tag = data["tag_name"]
    expected_url = f"https://github.com/{REPOSITORY}/releases/download/{tag}/{ASSET_NAME}"
    for asset in data.get("assets", ()):
        if asset.get("name") != ASSET_NAME:
            continue
        digest, size = asset.get("digest") or "", asset.get("size")
        if (asset.get("browser_download_url") == expected_url and
                re.fullmatch(r"sha256:[a-f0-9]{64}", digest) and
                type(size) is int and 0 < size <= MAX_ARCHIVE):
            return Release(tag, expected_url, digest[7:], size)
    raise ValueError(f"{tag} 缺少带 SHA-256 校验值的更新包")


def _digest(data):
    return hashlib.sha256(data).hexdigest()


def _read_package(archive, release):
    if len(archive) != release.size or _digest(archive) != release.digest:
        raise ValueError("更新包大小或 SHA-256 校验失败")
    with zipfile.ZipFile(io.BytesIO(archive)) as package:
        entries = package.infolist()
        if (sorted(item.filename for item in entries) != sorted(APP_FILES) or
                sum(item.file_size for item in entries) > MAX_CONTENTS):
            raise ValueError("更新包文件列表或大小无效")
        files = {name: package.read(name) for name in APP_FILES}
    for name, content in files.items():
        compile(content, name, "exec")
    version = _source_version(files["app_update.py"])
    if version_number(version) != version_number(release.tag):
        raise ValueError("更新包版本与发布标签不符")
    return files


def _source_version(code):
    for stmt in ast.parse(code).body:
        if isinstance(stmt, ast.Assign) and any(
                isinstance(target, ast.Name) and target.id == "APP_VERSION" for target in stmt.targets):
            return ast.literal_eval(stmt.value)
    raise ValueError("更新包缺少版本号")


def prepare_update(release, installation):
    original = {name: _digest((installation / name).read_bytes()) for name in APP_FILES}
    archive = _get(release.url, release.size)
    _read_package(archive, release)
    update_dir = installation / ".data" / "updates"
    update_dir.mkdir(parents=True, exist_ok=True)
    work = Path(tempfile.mkdtemp(prefix=release.tag + "-", dir=update_dir))
    try:
        (work / "payload.zip").write_bytes(archive)
        (work / "plan.json").write_text(json.dumps({"release": release._asdict(), "original": original}),
                                       encoding="utf-8")
        # 由当前已安装的更新程序执行替换，即使更新包也包含新版更新程序。
        shutil.copyfile(__file__, work / "apply_update.py")
    except OSError:
        shutil.rmtree(work, ignore_errors=True)
        raise
    return work


def install_update(work, installation, restart):
    recover_updates(installation)
    plan = json.loads((work / "plan.json").read_text(encoding="utf-8"))
    release = Release(**plan["release"])
    files = _read_package((work / "payload.zip").read_bytes(), release)
    backup = work / "backup"
    backup.mkdir()
    for name in APP_FILES:
        current = (installation / name).read_bytes()
        if _digest(current) != plan["original"][name]:
            raise ValueError(f"下载后 {name} 已改动，请重新检查更新")
        (backup / name).write_bytes(current)
        (work / name).write_bytes(files[name])

    journal = work / "in-progress.json"
    temp_journal = work / "in-progress.tmp"
    transaction = {"original": plan["original"], "updated": {name: _digest(files[name]) for name in APP_FILES}}
    temp_journal.write_text(json.dumps(transaction), encoding="utf-8")
    os.replace(temp_journal, journal)
    try:
        for name in APP_FILES:
            os.replace(work / name, installation / name)
        restart()
    except Exception:
        _recover_transaction(journal, installation)
        raise
    journal.unlink()


def _recover_transaction(journal, installation):
    work = journal.parent
    transaction = json.loads(journal.read_text(encoding="utf-8"))
    original, updated = transaction["original"], transaction["updated"]
    restore = []
    for name in APP_FILES:
        backup = work / "backup" / name
        if _digest(backup.read_bytes()) != original[name]:
            raise ValueError(f"无法恢复更新，备份的 {name} 已改动")
        target = installation / name
        digest = _digest(target.read_bytes()) if target.exists() else None
        if digest == original[name]:
            continue
        if digest is not None and digest != updated[name]:
            raise ValueError(f"无法自动恢复，{name} 在更新中断后已被编辑")
        restore.append((target, backup))
    for target, backup in restore:
        staged = work / (target.name + ".restore")
        shutil.copyfile(backup, staged)
        os.replace(staged, target)
    journal.unlink()


def _unfinished_updates(installation):
    journals = (installation / ".data" / "updates").glob("*/in-progress.json")
    return sorted(journal for journal in journals if re.fullmatch(
        r"v?(?:0|[1-9]\d{0,4})(?:\.(?:0|[1-9]\d{0,4})){2}-[a-z0-9_]+",
        journal.parent.name, re.ASCII))


def recover_updates(installation):
    for journal in _unfinished_updates(installation):
        _recover_transaction(journal, installation)


def recover_before_import(installation, mutex_name):
    """Recover a pending update before loading potentially mixed app modules."""
    if not _unfinished_updates(installation):
        return
    import ctypes
    import time
    from ctypes import wintypes as wt

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.CreateMutexW.argtypes = [ctypes.c_void_p, wt.BOOL, wt.LPCWSTR]
    kernel.CreateMutexW.restype = wt.HANDLE
    kernel.CloseHandle.argtypes = [wt.HANDLE]
    deadline = time.monotonic() + 45
    while _unfinished_updates(installation):
        mutex = kernel.CreateMutexW(None, False, mutex_name)
        if not mutex:
            raise ctypes.WinError(ctypes.get_last_error())
        if ctypes.get_last_error() != 183:
            try:
                recover_updates(installation)
            finally:
                kernel.CloseHandle(mutex)
            return
        kernel.CloseHandle(mutex)
        if time.monotonic() >= deadline:
            raise OSError("请先从托盘退出正在运行的外设电量，再重新启动以恢复中断的更新。")
        time.sleep(0.1)


def start_install(work, installation, mutex_name):
    subprocess.Popen([sys.executable, "-B", str(work / "apply_update.py"), "--apply",
                      str(installation), str(os.getpid()), mutex_name],
                     cwd=installation)


def _apply_on_windows(installation, pid, mutex_name):
    import ctypes
    from ctypes import wintypes as wt

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.OpenProcess.argtypes = [wt.DWORD, wt.BOOL, wt.DWORD]
    kernel.OpenProcess.restype = wt.HANDLE
    kernel.WaitForSingleObject.argtypes = [wt.HANDLE, wt.DWORD]
    kernel.WaitForSingleObject.restype = wt.DWORD
    kernel.CloseHandle.argtypes = [wt.HANDLE]
    kernel.CreateMutexW.argtypes = [ctypes.c_void_p, wt.BOOL, wt.LPCWSTR]
    kernel.CreateMutexW.restype = wt.HANDLE
    work = Path(__file__).resolve().parent

    def restart():
        subprocess.Popen([sys.executable, "-B", str(installation / "peripheral_battery_tray.py"),
                          "--after-update"], cwd=installation)

    mutex = None
    try:
        previous = kernel.OpenProcess(0x00100000, False, pid)
        if previous:
            try:
                if kernel.WaitForSingleObject(previous, 30000) != 0:
                    raise OSError("托盘程序未能退出")
            finally:
                kernel.CloseHandle(previous)
        elif ctypes.get_last_error() != 87:
            raise ctypes.WinError(ctypes.get_last_error())
        mutex = kernel.CreateMutexW(None, False, mutex_name)
        if not mutex or ctypes.get_last_error() == 183:
            raise OSError("无法锁定程序目录，已有实例运行")
        try:
            install_update(work, installation, restart)
        except Exception:
            restart()
            raise
    except Exception as exc:
        if mutex:
            kernel.CloseHandle(mutex)
            mutex = None
        ctypes.windll.user32.MessageBoxW(None, f"{exc}\n\n更新数据保存在 {work}",
                                        "外设电量更新失败", 0x10)
        return 1
    finally:
        if mutex:
            kernel.CloseHandle(mutex)
    return 0


def build_package(tag, output, source=None):
    if version_number(tag) != version_number(APP_VERSION):
        raise ValueError("发布标签必须与 APP_VERSION 一致")
    source = source or Path(__file__).resolve().parent
    output.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as archive:
        for name in APP_FILES:
            archive.write(source / name, name)


if __name__ == "__main__":
    action, *args = sys.argv[1:]
    if action == "--apply":
        sys.exit(_apply_on_windows(Path(args[0]), int(args[1]), args[2]))
    if action == "--build":
        build_package(args[0], Path(args[1]))
    else:
        sys.exit("用法：app_update.py --build vX.Y.Z 输出路径")
