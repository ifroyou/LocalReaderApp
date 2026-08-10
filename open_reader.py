from pathlib import Path
import argparse
import json
import os
import secrets
import shutil
import subprocess
import sys
import time
import urllib.request
import webbrowser

APP_DIR = Path(__file__).resolve().parent
PORT = 8765
APP_BUILD_ID = "localreader-v3.2-final-20260713-05"
APP_URL = f"http://127.0.0.1:{PORT}/?v={APP_BUILD_ID}"
HEALTH_URL = f"http://127.0.0.1:{PORT}/api/health"
LOCALAPPDATA = os.environ.get("LOCALAPPDATA", "")
RUNTIME_DIR = Path(LOCALAPPDATA) / "LocalReaderApp" if LOCALAPPDATA else APP_DIR / ".runtime"
LOG = RUNTIME_DIR / "reader_server.log"
LEGACY_EDGE_PROFILE = APP_DIR / "edge_app_profile_window"
LEGACY_EDGE_PROFILES = (
    LEGACY_EDGE_PROFILE,
    APP_DIR / "edge_app_profile",
    APP_DIR / "edge_app_profile_app",
    APP_DIR / "edge_app_profile_clean",
    APP_DIR / "edge_app_profile_final",
)
EDGE_PROFILE_SEED = APP_DIR / "edge_profile_seed"
EDGE_PROFILE = RUNTIME_DIR / "edge_app_profile_window"
EDGE_PATHS = [
    Path(r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"),
    Path(r"C:\Program Files\Microsoft\Edge\Application\msedge.exe"),
]
CREATE_NO_WINDOW = 0x08000000 if os.name == "nt" else 0


def ensure_runtime_dir():
    RUNTIME_DIR.mkdir(parents=True, exist_ok=True)


def rotate_runtime_log(max_bytes=8 * 1024 * 1024):
    ensure_runtime_dir()
    try:
        if not LOG.exists() or LOG.stat().st_size <= max_bytes:
            return
        older = LOG.with_name(LOG.name + ".2")
        previous = LOG.with_name(LOG.name + ".1")
        older.unlink(missing_ok=True)
        if previous.exists():
            previous.replace(older)
        LOG.replace(previous)
    except OSError:
        pass


def migrate_legacy_edge_profile():
    """Seed a per-machine Edge profile once; runtime files never sync afterward."""
    ensure_runtime_dir()
    source_profile = next(
        (candidate for candidate in (EDGE_PROFILE_SEED, LEGACY_EDGE_PROFILE) if candidate.is_dir()),
        None,
    )
    if EDGE_PROFILE.exists() or source_profile is None:
        return

    staging = RUNTIME_DIR / "edge_app_profile_window.migrating"
    volatile_names = {
        "Cache",
        "Code Cache",
        "GPUCache",
        "DawnCache",
        "GrShaderCache",
        "ShaderCache",
        "Crashpad",
        "Crash Reports",
        "BrowserMetrics",
    }

    def ignore_volatile(_directory, names):
        return [
            name
            for name in names
            if name in volatile_names or name.startswith("Singleton")
        ]

    def copy_unlocked_file(source, destination):
        try:
            return shutil.copy2(source, destination)
        except OSError:
            # Edge may briefly lock a history/cache file. The profile remains
            # usable without that individual file, and the OneDrive copy stays intact.
            return destination

    try:
        shutil.copytree(
            source_profile,
            staging,
            dirs_exist_ok=True,
            copy_function=copy_unlocked_file,
            ignore=ignore_volatile,
        )
    except (OSError, shutil.Error):
        pass

    if staging.exists() and not EDGE_PROFILE.exists():
        try:
            staging.replace(EDGE_PROFILE)
        except OSError:
            pass


def focus_existing_window():
    if os.name != "nt":
        return False
    profile = str(EDGE_PROFILE).replace("'", "''")
    script = rf"""
$profile = '{profile}'
$p = Get-CimInstance Win32_Process -Filter "Name='msedge.exe'" | Where-Object {{
  $_.CommandLine -and $_.CommandLine -like "*$profile*"
}} | ForEach-Object {{ Get-Process -Id $_.ProcessId -ErrorAction SilentlyContinue }} | Where-Object {{ $_.MainWindowTitle -match 'Local Reader' }} | Select-Object -First 1
if ($p) {{
  $ws = New-Object -ComObject WScript.Shell
  $null = $ws.AppActivate($p.Id)
  exit 0
}}
exit 1
"""
    try:
        result = subprocess.run(
            ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", script],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=CREATE_NO_WINDOW,
            timeout=3,
        )
        return result.returncode == 0
    except Exception:
        return False



def url_text(url, timeout=2):
    with urllib.request.urlopen(url, timeout=timeout) as response:
        return response.read().decode("utf-8", errors="replace")


def app_health(timeout=2):
    try:
        health = json.loads(url_text(HEALTH_URL, timeout=timeout))
        return health if isinstance(health, dict) else None
    except Exception:
        return None


def health_belongs_to_app(health):
    return bool(
        isinstance(health, dict)
        and health.get("app_root") == str(APP_DIR)
    )


def health_matches_app(health):
    return bool(
        health_belongs_to_app(health)
        and health.get("app_build") == APP_BUILD_ID
        and health.get("vieneu_enabled") is True
        and health.get("tts_engine") == "vieneu"
    )


def app_is_ready(timeout=2):
    return health_matches_app(app_health(timeout=timeout))


def wait_for_owned_server_ready(timeout=45):
    """Do not kill our server just because a cold library merge exceeds 2 seconds."""
    health = app_health(timeout=2)
    if health is not None:
        # A responsive old build from this folder is safe to replace. A server
        # from another program will be rejected by stop_port().
        return health_matches_app(health) if health_belongs_to_app(health) else False
    owners = set(pids_on_port(PORT))
    if not owners:
        return False
    allowed = set(local_reader_pids())
    if owners - allowed:
        return False

    deadline = time.time() + max(0, timeout)
    while time.time() < deadline:
        health = app_health(timeout=2)
        if health is not None:
            # A responsive but mismatched/old build must be replaced.
            return health_matches_app(health)
        time.sleep(0.5)
    return False


def pids_on_port(port):
    if os.name != "nt":
        return []
    try:
        raw = subprocess.check_output(["netstat", "-ano"], text=True, errors="ignore")
    except Exception:
        return []
    pids = set()
    for line in raw.splitlines():
        parts = line.split()
        local_port = parts[1].rsplit(":", 1)[-1] if len(parts) >= 2 else ""
        if len(parts) >= 5 and local_port == str(port) and parts[3].upper() == "LISTENING":
            try:
                pids.add(int(parts[-1]))
            except ValueError:
                pass
    return sorted(pids)


def stop_port(port):
    owners = set(pids_on_port(port))
    allowed = set(local_reader_pids())
    unexpected = owners - allowed
    if unexpected and health_belongs_to_app(app_health(timeout=3)):
        unexpected.clear()
    if unexpected:
        raise RuntimeError(f"Port {port} is being used by another program (PID {', '.join(map(str, sorted(unexpected)))}). Close it, then open Local Reader again.")
    for pid in sorted(owners):
        try:
            subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except Exception:
            pass


def reader_runtime_python():
    candidates = [
        Path(os.environ.get("LOCAL_READER_VIENEU_PYTHON", "")) if os.environ.get("LOCAL_READER_VIENEU_PYTHON") else None,
        APP_DIR / "python" / "python.exe",
        APP_DIR / ".vieneu_test" / "Scripts" / "python.exe",
        Path(LOCALAPPDATA) / "LocalReaderApp" / ".vieneu_test" / "Scripts" / "python.exe" if LOCALAPPDATA else None,
        Path(sys.executable),
    ]
    for candidate in candidates:
        if candidate and candidate.exists():
            return candidate
    return Path(sys.executable)


def local_reader_pids():
    return [pid for pid, _parent in local_reader_process_rows()]


def local_reader_process_rows():
    if os.name != "nt":
        return []
    root = str(APP_DIR)
    script = f"""
$root = @'
{root}
'@
Get-CimInstance Win32_Process | Where-Object {{
  $_.ProcessId -ne {os.getpid()} -and
  $_.CommandLine -and
  $_.CommandLine.IndexOf($root, [StringComparison]::OrdinalIgnoreCase) -ge 0 -and
  ($_.CommandLine -like "*reader_server.py*" -or $_.CommandLine -like "*vieneu_worker.py*")
}} | ForEach-Object {{ "$($_.ProcessId),$($_.ParentProcessId)" }}
"""
    try:
        raw = subprocess.check_output(
            ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", script],
            text=True,
            errors="ignore",
            creationflags=CREATE_NO_WINDOW,
            timeout=15,
        )
        rows = []
        for line in raw.splitlines():
            parts = [part.strip() for part in line.split(",", 1)]
            if len(parts) == 2 and parts[0].isdigit() and parts[1].isdigit():
                rows.append((int(parts[0]), int(parts[1])))
        return rows
    except Exception:
        return []


def stop_reader_processes(pids):
    for pid in sorted(set(int(pid) for pid in pids if int(pid) > 0)):
        try:
            subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except Exception:
            pass


def stop_orphan_reader_processes():
    listening = set()
    for port in (8765, 8766, 8767, 8768, 8769):
        listening.update(pids_on_port(port))
    rows = local_reader_process_rows()
    parents = {pid: parent for pid, parent in rows}
    keep = set(listening)
    changed = True
    while changed:
        changed = False
        for pid, parent in parents.items():
            if pid in keep and parent in parents and parent not in keep:
                keep.add(parent)
                changed = True
            if parent in keep and pid not in keep:
                keep.add(pid)
                changed = True
    stop_reader_processes(pid for pid, _parent in rows if pid not in keep)


def start_server():
    stop_orphan_reader_processes()
    if app_is_ready():
        return False
    if wait_for_owned_server_ready(timeout=45):
        return False
    # If another old reader server owns 8765, replace it with this app-folder server.
    stop_port(PORT)
    stop_reader_processes(local_reader_pids())
    time.sleep(0.8)
    ensure_runtime_dir()
    rotate_runtime_log()
    log = open(LOG, "a", encoding="utf-8", buffering=1)
    log.write("\n--- start Local Reader App ---\n")
    python = reader_runtime_python()
    env = os.environ.copy()
    env["LOCAL_READER_RUNTIME_DIR"] = str(RUNTIME_DIR)
    env["LOCAL_READER_AUDIO_DIR"] = str(RUNTIME_DIR / "reader_audio_cache")
    env["LOCAL_READER_VIENEU_PORTS"] = "8766"
    env["LOCAL_READER_VIENEU_ENABLED"] = "1"
    env["LOCAL_READER_BACKGROUND_WORKERS"] = "1"
    env["LOCAL_READER_TTS_MODEL_VERSION"] = "vieneu-tts-v3-turbo-48khz"
    env["LOCAL_READER_VIENEU_MODEL_VERSION"] = "vieneu-tts-v3-turbo-48khz"
    env["LOCAL_READER_VIENEU_MODEL_FORMAT"] = "v3-turbo"
    env["LOCAL_READER_VIENEU_MODE"] = "v3turbo"
    # Let each machine pick the best local backend: NVIDIA/CUDA when available,
    # otherwise the CPU/ONNX path. This keeps the synced app portable.
    env["LOCAL_READER_VIENEU_BACKBONE_DEVICE"] = "auto"
    env["LOCAL_READER_VIENEU_BACKEND"] = "auto"
    env["LOCAL_READER_VIENEU_BACKBONE_REPO"] = "pnnbao-ump/VieNeu-TTS-v3-Turbo"
    env["LOCAL_READER_VIENEU_VOICE"] = "Trúc Ly"
    env["LOCAL_READER_VIENEU_VOICE_LABEL"] = "Trúc Ly [VieNeu v3]"
    env["LOCAL_READER_VIENEU_GGUF_FILENAME"] = ""
    env["LOCAL_READER_WORKER_TOKEN"] = secrets.token_urlsafe(32)
    env["HF_HUB_DISABLE_XET"] = "1"
    env["PYTHONUNBUFFERED"] = "1"
    subprocess.Popen(
        [str(python), "-u", "-B", str(APP_DIR / "reader_server.py")],
        cwd=str(APP_DIR),
        stdout=log,
        stderr=subprocess.STDOUT,
        env=env,
        creationflags=CREATE_NO_WINDOW,
    )
    deadline = time.time() + 90
    while time.time() < deadline:
        if app_is_ready():
            return True
        time.sleep(0.25)
        time.sleep(1)
    raise RuntimeError(f"Local Reader server did not become ready. Check {LOG}")


def stop_edge_profile():
    if os.name != "nt":
        return
    profiles = [str(EDGE_PROFILE), *(str(path) for path in LEGACY_EDGE_PROFILES)]
    powershell_profiles = ", ".join(
        "'" + profile.replace("'", "''") + "'" for profile in profiles
    )
    script = f"""
$profiles = @({powershell_profiles})
Get-CimInstance Win32_Process | Where-Object {{
  $process = $_
  $matchesProfile = $false
  foreach ($profile in $profiles) {{
    if ([string]$process.CommandLine -like "*$profile*") {{ $matchesProfile = $true; break }}
  }}
  $process.Name -eq 'msedge.exe' -and $matchesProfile
}} | ForEach-Object {{
  try {{ Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }} catch {{}}
}}
"""
    try:
        subprocess.run(
            ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", script],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=CREATE_NO_WINDOW,
            timeout=5,
        )
        time.sleep(0.6)
    except Exception:
        pass


def open_desktop_window(force_restart=False):
    migrate_legacy_edge_profile()
    if not force_restart and focus_existing_window():
        return
    stop_edge_profile()
    EDGE_PROFILE.mkdir(parents=True, exist_ok=True)
    if os.name == "nt":
        for edge in EDGE_PATHS:
            if edge.exists():
                subprocess.Popen(
                    [
                        str(edge),
                        f"--app={APP_URL}",
                        f"--user-data-dir={EDGE_PROFILE}",
                        "--no-first-run",
                        "--disable-background-mode",
                        "--disable-sync",
                        "--disable-translate",
                        "--disable-features=msEdgeSync,Translate",
                    ],
                    cwd=str(APP_DIR),
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    creationflags=CREATE_NO_WINDOW,
                )
                return
    webbrowser.open(APP_URL)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--no-open", action="store_true")
    args = parser.parse_args()
    server_restarted = start_server()
    # Do not block or warm /api/voices here: workers may be busy generating audio.
    # The UI can open as soon as /api/health is OK and will check VieNeu itself.
    if not args.no_open:
        open_desktop_window(force_restart=server_restarted)
    print(APP_URL)


if __name__ == "__main__":
    main()
