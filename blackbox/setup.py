"""Automatic isolated runtime and official Python dependency installation."""
from __future__ import annotations
import importlib
import importlib.metadata
import hashlib
import json
import os
import platform
import queue
import struct
import subprocess
import sys
import sysconfig
import tempfile
import threading
import time
import traceback
import urllib.parse
import urllib.request
import venv
import zipfile
from pathlib import Path
from .resources import TITLE, InstallerError, data_root, log_root, digest, entrypoint, application_dir

DEPS = tuple(line.strip() for line in (application_dir() / "requirements.txt").read_text(encoding="utf-8").splitlines()
             if line.strip() and not line.lstrip().startswith("#"))

UV_VERSION = "0.12.23"

UV_WHEELS = {
    ("Windows", "x86_64"): ("win_amd64", "fb8a4117a5224d73abe2204a1e744ae34b544f1b9a0b761576851deaa7215c12"),
    ("Darwin", "x86_64"): ("macosx_10_12_x86_64", "4951286e68f116fa41897ecf8e248cc2836cb013be6d4f37afae4049f9ef4df7"),
    ("Darwin", "aarch64"): ("macosx_11_0_arm64", "99197c022afbd1edd0a295e9fdac5078c61eddf2e77657a77294cfe8ea0d94b9"),
    ("Linux", "x86_64"): ("manylinux_2_17_x86_64.manylinux2014_x86_64", "565c6e2874dbeae86c02f3dea97255e878fec672659a73d4930c6b93fcab2fff"),
    ("Linux", "aarch64"): ("manylinux_2_17_aarch64.manylinux2014_aarch64.musllinux_1_1_aarch64", "895137194d242cc8075c3006288b485af54feeae68512f4ffc96f75b27543cc0"),
}


def dependencies_ready() -> bool:
    try:
        for requirement in DEPS:
            package, version = requirement.split("==")
            if importlib.metadata.version(package) != version:
                return False
        modules = [importlib.import_module(n) for n in ("PySide6.QtWidgets", "rtmidi", "hid")]
        return (hasattr(modules[0], "QApplication") and hasattr(modules[1], "MidiIn")
                and hasattr(modules[1], "MidiOut") and hasattr(modules[2], "device"))
    except (ImportError, OSError, importlib.metadata.PackageNotFoundError):
        return False


def compatible_python() -> bool:
    return (platform.python_implementation() == "CPython"
            and (3, 10) <= sys.version_info[:2] <= (3, 12)
            and struct.calcsize("P") == 8
            and not sysconfig.get_config_var("Py_GIL_DISABLED"))


def managed_platform() -> tuple[str, str, str]:
    system = platform.system()
    arch = platform.machine().lower()
    if system == "Windows":
        # A 32-bit launcher on 64-bit Windows must download a 64-bit runtime.
        arch = (os.environ.get("PROCESSOR_ARCHITEW6432")
                or os.environ.get("PROCESSOR_ARCHITECTURE") or arch).lower()
    arch = {"amd64": "x86_64", "arm64": "aarch64"}.get(arch, arch)
    if system == "Windows" and arch == "aarch64":
        # The pinned Qt and MIDI wheels use x64 Windows, including x64 emulation.
        arch = "x86_64"
    if (system, arch) not in UV_WHEELS:
        raise InstallerError(f"No automatic desktop runtime is available for {system} {arch}. "
                             "Use a 64-bit Windows, macOS or Linux desktop.")
    libc = "gnu" if system == "Linux" else "none"
    if system == "Linux" and platform.libc_ver()[0].lower() == "musl":
        raise InstallerError("The Qt/MIDI binary packages require a glibc Linux desktop; "
                             "automatic setup is unavailable on musl/Alpine.")
    return system, arch, libc


def setup_environment() -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if not k.startswith("UV_")}
    for key in ("PYTHONHOME", "PYTHONPATH", "PYTHONSTARTUP", "VIRTUAL_ENV", "BLACKBOX_READY"):
        env.pop(key, None)
    return env


def setup_process(command, log, stop, env, timeout=300):
    if stop.is_set():
        raise InstallerError("Setup cancelled.")
    log.write("\nRunning: "+json.dumps([str(p) for p in command])+"\n")
    log.flush()
    process = subprocess.Popen(command, stdout=log, stderr=log, env=env,
                               creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
    deadline = time.monotonic()+timeout
    try:
        while process.poll() is None:
            if stop.wait(0.1):
                raise InstallerError("Setup cancelled.")
            if time.monotonic() > deadline:
                raise InstallerError("Automatic setup timed out. Check your internet connection and try again.")
        if process.returncode:
            raise InstallerError("Automatic setup failed. Check internet access and the setup log.")
    finally:
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)


def download_runtime_tool(root, target, progress, stop) -> Path:
    system, arch, _ = target
    tag, expected_hash = UV_WHEELS[system, arch]
    filename = f"uv-{UV_VERSION}-py3-none-{tag}.whl"
    tool = root / "tools" / f"uv-{UV_VERSION}-{system}-{arch}" / ("uv.exe" if system == "Windows" else "uv")
    if tool.is_file() and tool.stat().st_size:
        return tool
    progress.put("Downloading runtime setup tool…")
    request = urllib.request.Request(f"https://pypi.org/pypi/uv/{UV_VERSION}/json",
                                     headers={"User-Agent": "annblackbox-custom-firmware-installer/1"})
    with urllib.request.urlopen(request, timeout=30) as response:
        metadata = json.loads(response.read(2*1024*1024))
    candidates = [f for f in metadata.get("urls", []) if f.get("filename") == filename
                  and f.get("digests", {}).get("sha256") == expected_hash and not f.get("yanked")]
    if len(candidates) != 1:
        raise InstallerError("Could not verify the official runtime setup package.")
    item = candidates[0]
    url = urllib.parse.urlparse(item["url"])
    if url.scheme != "https" or url.hostname != "files.pythonhosted.org":
        raise InstallerError("Unexpected runtime package download location.")
    expected_size = int(item["size"])
    if not 0 < expected_size < 120*1024*1024:
        raise InstallerError("Unexpected runtime package size.")
    tool.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="download-", dir=tool.parent) as scratch:
        wheel = Path(scratch) / "runtime.whl"
        checksum = hashlib.sha256()
        received = 0
        deadline = time.monotonic()+180
        with urllib.request.urlopen(item["url"], timeout=30) as response, wheel.open("wb") as out:
            while True:
                if stop.is_set():
                    raise InstallerError("Setup cancelled.")
                if time.monotonic() > deadline:
                    raise InstallerError("Runtime download timed out. Check your internet connection.")
                block = response.read(256*1024)
                if not block:
                    break
                received += len(block)
                if received > expected_size:
                    raise InstallerError("Unexpected runtime download size.")
                checksum.update(block)
                out.write(block)
                progress.put(f"Downloading runtime setup tool… {received*100//expected_size}%")
        if received != expected_size or checksum.hexdigest() != expected_hash:
            raise InstallerError("Runtime download was incomplete or failed its SHA-256 check. Try again.")
        member = f"uv-{UV_VERSION}.data/scripts/"+tool.name
        with zipfile.ZipFile(wheel) as archive:
            info = archive.getinfo(member)
            if info.is_dir() or not 0 < info.file_size < 160*1024*1024:
                raise InstallerError("Unexpected runtime tool in the verified package.")
            data = archive.read(info)
        staged = Path(scratch) / tool.name
        staged.write_bytes(data)
        staged.chmod(0o700)
        staged.replace(tool)
    return tool


def runtime_ready(python: Path) -> bool:
    if not python.is_file():
        return False
    try:
        result = subprocess.run([str(python), "-I", str(entrypoint()), "--check-dependencies"],
                                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                env=setup_environment(), timeout=20,
                                creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
        return result.returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def runtime_has_pip(python: Path) -> bool:
    if not python.is_file():
        return False
    try:
        result = subprocess.run([str(python), "-I", "-m", "pip", "--version"],
                                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                env=setup_environment(), timeout=20,
                                creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
        return result.returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def launch_runtime(python: Path):
    env = dict(setup_environment(), BLACKBOX_READY="1")
    executable = python
    if os.name == "nt" and sys.stdout is None and python.with_name("pythonw.exe").is_file():
        executable = python.with_name("pythonw.exe")
    command = [str(executable), "-I", str(entrypoint()), *sys.argv[1:]]
    raise SystemExit(subprocess.call(command, env=env,
                                    creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0))


def ensure_dependencies():
    native = compatible_python()
    if native and dependencies_ready():
        return
    if os.environ.get("BLACKBOX_READY") == "1":
        raise InstallerError("Dependency installation did not complete. See setup.log in the application data folder.")
    root = data_root()
    target = None if native else managed_platform()
    name = ("runtime-"+platform.system()+"-"+platform.machine()+f"-{sys.version_info.major}.{sys.version_info.minor}"
            if native else f"runtime-managed-3.12-{target[0]}-{target[1]}")
    envdir = root / name
    python = envdir / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    if runtime_ready(python):
        launch_runtime(python)
    setup_log = log_root() / "setup.log"
    progress = queue.Queue()
    stop = threading.Event()
    done = threading.Event()
    failure = []

    def setup():
        try:
            progress.put("Preparing Python dependencies…")
            with setup_log.open("w", encoding="utf-8") as log:
                log.write(f"Launcher: {platform.python_implementation()} {sys.version} ({struct.calcsize('P')*8}-bit)\n")
                log.write(f"Application runtime: {envdir}\n")
                env = setup_environment()
                if not runtime_has_pip(python):
                    if native:
                        venv.EnvBuilder(with_pip=True).create(envdir)
                    else:
                        tool = download_runtime_tool(root, target, progress, stop)
                        env.update(UV_PYTHON_INSTALL_DIR=str(root / "python"), UV_CACHE_DIR=str(root / "cache"),
                                   UV_PYTHON_DOWNLOADS="automatic", UV_NO_CONFIG="1", UV_HTTP_TIMEOUT="30")
                        progress.put("Downloading compatible Python automatically…")
                        system, arch, libc = target
                        request = f"cpython-3.12-{system.lower() if system != 'Darwin' else 'macos'}-{arch}-{libc}"
                        setup_process([str(tool), "venv", "--no-config", "--no-project", "--managed-python", "--allow-existing",
                                       "--python", request, "--seed", "--default-index", "https://pypi.org/simple",
                                       str(envdir)], log, stop, env)
                command = [str(python), "-I", "-m", "pip", "--isolated", "install", "--disable-pip-version-check", "--only-binary=:all:",
                           "--retries", "2", "--timeout", "30", "--index-url", "https://pypi.org/simple", *DEPS]
                progress.put("Installing dependencies…")
                setup_process(command, log, stop, env)
                if not runtime_ready(python):
                    raise InstallerError("The installed libraries could not load. Check OS libraries and the setup log.")
        except Exception as exc:
            try:
                with setup_log.open("a", encoding="utf-8") as log:
                    log.write(traceback.format_exc())
            except OSError:
                pass
            failure.append(f"{exc}\n\nDetected: {platform.python_implementation()} "
                           f"{sys.version_info.major}.{sys.version_info.minor}, {struct.calcsize('P')*8}-bit.\n"
                           f"Setup log: {setup_log}")
        finally:
            done.set()

    thread = threading.Thread(target=setup, daemon=False)
    thread.start()
    # A stdlib splash makes first-run setup visible; Qt replaces it after setup.
    splash = None
    try:
        import tkinter as tk
        from tkinter import ttk
        splash = tk.Tk()
        splash.title(TITLE)
        splash.geometry("440x170")
        splash.resizable(False, False)
        label = ttk.Label(splash, text="Installing dependencies…", anchor="center")
        label.pack(fill="x", padx=20, pady=(25, 14))
        bar = ttk.Progressbar(splash, mode="indeterminate")
        bar.pack(fill="x", padx=30)
        bar.start()
        ttk.Button(splash, text="Cancel", command=stop.set).pack(pady=15)
        splash.protocol("WM_DELETE_WINDOW", stop.set)
        while not done.is_set():
            while not progress.empty():
                label.configure(text=progress.get_nowait())
            splash.update()
            time.sleep(0.02)
    except Exception:
        while not done.wait(0.25):
            while not progress.empty():
                message = progress.get_nowait()
                if sys.stdout:
                    print(message, flush=True)
    finally:
        if splash:
            try:
                splash.destroy()
            except Exception:
                pass
    thread.join()
    if stop.is_set():
        raise SystemExit(0)
    if failure:
        raise InstallerError(failure[0])
    launch_runtime(python)

