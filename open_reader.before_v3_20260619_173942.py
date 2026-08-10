from pathlib import Path
import argparse
import json
import os
import subprocess
import sys
import time
import urllib.request
import webbrowser

APP_DIR = Path(__file__).resolve().parent
PORT = 8765
APP_URL = f"http://127.0.0.1:{PORT}/?v=localreader_audio_plan_fix_20260615_1515"
HEALTH_URL = f"http://127.0.0.1:{PORT}/api/health"
LOG = APP_DIR / "reader_server.log"
EDGE_PROFILE = APP_DIR / "edge_app_profile_window"
LOCALAPPDATA = os.environ.get("LOCALAPPDATA", "")
EDGE_PATHS = [
    Path(r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"),
    Path(r"C:\Program Files\Microsoft\Edge\Application\msedge.exe"),
]
CREATE_NO_WINDOW = 0x08000000 if os.name == "nt" else 0


def focus_existing_window():
    if os.name != "nt":
        return False
    script = r"""
$p = Get-Process msedge -ErrorAction SilentlyContinue | Where-Object { $_.MainWindowTitle -match 'Local Reader' } | Select-Object -First 1
if ($p) {
  $ws = New-Object -ComObject WScript.Shell
  $null = $ws.AppActivate($p.Id)
  exit 0
}
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


def app_is_ready():
    try:
        root_html = url_text(APP_URL, timeout=2)
        phone_html = url_text(f"http://127.0.0.1:{PORT}/phone", timeout=2)
        health = json.loads(url_text(HEALTH_URL, timeout=2))
        return (
            "Local Reader" in root_html
            and "Local Reader Phone" in phone_html
            and health.get("app_root") == str(APP_DIR)
            and health.get("vieneu_enabled") is True
            and health.get("tts_engine") == "vieneu"
        )
    except Exception:
        return False


def pids_on_port(port):
    if os.name != "nt":
        return []
    try:
        raw = subprocess.check_output(["netstat", "-ano"], text=True, errors="ignore")
    except Exception:
        return []
    pids = set()
    needle = f":{port}"
    for line in raw.splitlines():
        parts = line.split()
        if len(parts) >= 5 and needle in parts[1] and parts[3].upper() == "LISTENING":
            try:
                pids.add(int(parts[-1]))
            except ValueError:
                pass
    return sorted(pids)


def stop_port(port):
    for pid in pids_on_port(port):
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
  $_.CommandLine -like "*$root*" -and
  ($_.CommandLine -like "*reader_server.py*" -or $_.CommandLine -like "*vieneu_worker.py*")
}} | ForEach-Object {{ "$($_.ProcessId),$($_.ParentProcessId)" }}
"""
    try:
        raw = subprocess.check_output(
            ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", script],
            text=True,
            errors="ignore",
            creationflags=CREATE_NO_WINDOW,
            timeout=5,
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
    stop_reader_processes(pid for pid, _parent in rows if pid not in keep)


def start_server():
    stop_orphan_reader_processes()
    if app_is_ready():
        return
    # If another old reader server owns 8765, replace it with this app-folder server.
    stop_port(PORT)
    stop_reader_processes(local_reader_pids())
    time.sleep(0.8)
    log = open(LOG, "a", encoding="utf-8", buffering=1)
    log.write("\n--- start Local Reader App ---\n")
    python = reader_runtime_python()
    env = os.environ.copy()
    env["LOCAL_READER_RUNTIME_DIR"] = str(Path(LOCALAPPDATA) / "LocalReaderApp") if LOCALAPPDATA else str(APP_DIR / ".runtime")
    env["LOCAL_READER_VIENEU_PORTS"] = "8766"
    env["LOCAL_READER_VIENEU_ENABLED"] = "1"
    env["LOCAL_READER_BACKGROUND_WORKERS"] = "1"
    env["LOCAL_READER_TTS_MODEL_VERSION"] = "vieneu-tts-0.3b-full-fp32"
    env["LOCAL_READER_VIENEU_MODEL_FORMAT"] = "full-fp32"
    env["LOCAL_READER_VIENEU_MODE"] = "standard"
    env["LOCAL_READER_VIENEU_BACKBONE_DEVICE"] = "cuda"
    env["LOCAL_READER_VIENEU_BACKBONE_REPO"] = "pnnbao-ump/VieNeu-TTS-0.3B"
    env["LOCAL_READER_VIENEU_GGUF_FILENAME"] = ""
    subprocess.Popen(
        [str(python), "-B", str(APP_DIR / "reader_server.py")],
        cwd=str(APP_DIR),
        stdout=log,
        stderr=subprocess.STDOUT,
        env=env,
        creationflags=CREATE_NO_WINDOW,
    )
    deadline = time.time() + 90
    while time.time() < deadline:
        if app_is_ready():
            return
        time.sleep(1)
    raise RuntimeError(f"Local Reader server did not become ready. Check {LOG}")


def stop_edge_profile():
    if os.name != "nt":
        return
    profile = str(EDGE_PROFILE)
    script = f"""
Get-CimInstance Win32_Process | Where-Object {{ $_.Name -eq 'msedge.exe' -and $_.CommandLine -like '*{profile}*' }} | ForEach-Object {{
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


def open_desktop_window():
    stop_edge_profile()
    EDGE_PROFILE.mkdir(exist_ok=True)
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
    start_server()
    # Do not block or warm /api/voices here: workers may be busy generating audio.
    # The UI can open as soon as /api/health is OK and will check VieNeu itself.
    if not args.no_open:
        open_desktop_window()
    print(APP_URL)


if __name__ == "__main__":
    main()
