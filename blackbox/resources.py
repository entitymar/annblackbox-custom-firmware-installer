"""External firmware, native files and application-local paths."""
from __future__ import annotations
import hashlib
import json
import os
from pathlib import Path

TITLE = "annblackbox-custom-firmware-installer"
STOCK_SHA = "c0fef191b860d3b20f77fc492a24986eef2d12df44bb5e38355684820e26cb08"
BRIDGE_SHA = "cd57271dcea394b9b342e9e757d082cbd6b2bec86b0dff9555ff7bb21821717a"
NATIVE_MANIFEST_SHA = "aebb226c8f3fd5602bb44eee12649504903088697701684b068303a631d867fd"
APP_DIR = Path(__file__).resolve().parents[1]

class InstallerError(Exception):
    pass
class Cancelled(InstallerError):
    pass
class TransportLost(InstallerError):
    pass

def application_dir() -> Path:
    return APP_DIR

def entrypoint() -> Path:
    return APP_DIR / "annblackbox-custom-firmware-installer.py"

def local_directory(name: str) -> Path:
    path = APP_DIR / name
    try:
        path.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise InstallerError(f"Cannot write inside the program folder: {path}. Extract the complete ZIP into a writable folder.") from exc
    return path

def data_root() -> Path:
    return local_directory("dependencies")

def log_root() -> Path:
    return local_directory("logs")

def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()

def factory_bytes() -> bytes:
    path = APP_DIR / "firmware" / "BlackBox_FACTORY_V20.fwsc"
    try:
        data = path.read_bytes()
    except OSError as exc:
        raise InstallerError(f"Original V20 file is missing or unreadable: {path}. Extract the complete ZIP.") from exc
    if digest(data) != STOCK_SHA:
        raise InstallerError("firmware/BlackBox_FACTORY_V20.fwsc failed its SHA-256 check. Restore the original file from this ZIP.")
    return data

def native_helper_path() -> Path:
    return APP_DIR / "native" / "windows" / "M-UPGRADE" / "M-UPGRADE.exe"

def verify_native_files() -> Path:
    folder = native_helper_path().parent
    try:
        manifest_bytes = (folder / "FILES.json").read_bytes()
        if digest(manifest_bytes) != NATIVE_MANIFEST_SHA:
            raise InstallerError("The native files manifest failed its SHA-256 check. Extract a clean copy of this ZIP.")
        manifest = json.loads(manifest_bytes)
        for item in manifest["files"]:
            path = folder / item["name"]
            if not path.resolve().is_relative_to(folder.resolve()):
                raise InstallerError("Invalid native resource path.")
            data = path.read_bytes()
            if len(data) != item["size"] or digest(data) != item["sha256"]:
                raise InstallerError(f"Native file failed its SHA-256 check: {item['name']}. Extract a clean copy of this ZIP.")
    except (OSError, KeyError, ValueError) as exc:
        raise InstallerError("Native M-UPGRADE helper or DLLs are missing. Extract the complete ZIP.") from exc
    return native_helper_path()
