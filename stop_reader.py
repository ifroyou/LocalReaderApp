import os
from pathlib import Path
import subprocess


APP_DIR = Path(__file__).resolve().parent
RUNTIME_DIR = Path(
    os.environ.get("LOCAL_READER_RUNTIME_DIR")
    or Path(os.environ.get("LOCALAPPDATA", APP_DIR)) / "LocalReaderApp"
).resolve()
EDGE_PROFILE = RUNTIME_DIR / "edge_app_profile_window"
PORTS = (8765, 8766, 8767, 8768, 8769)
CREATE_NO_WINDOW = 0x08000000 if os.name == "nt" else 0
APP_PROCESS_MARKERS = (
    "reader_server.py",
    "vieneu_worker.py",
    "gwen_worker.py",
    "open_reader.py",
)


def pids_on_port(port):
    """Return TCP listeners whose local port exactly equals *port*."""
    if os.name != "nt":
        return []
    try:
        raw = subprocess.check_output(
            ["netstat", "-ano", "-p", "tcp"],
            text=True,
            errors="ignore",
            creationflags=CREATE_NO_WINDOW,
        )
    except Exception:
        return []

    pids = set()
    expected_port = str(int(port))
    for line in raw.splitlines():
        parts = line.split()
        if len(parts) < 5 or parts[0].upper() != "TCP":
            continue
        if parts[-2].upper() != "LISTENING":
            continue
        _host, separator, local_port = parts[1].rpartition(":")
        if not separator or local_port != expected_port:
            continue
        try:
            pids.add(int(parts[-1]))
        except ValueError:
            pass
    return sorted(pids)


def app_process_pids():
    """Find only Python processes launched from this LocalReader app folder."""
    if os.name != "nt":
        return set()
    env = os.environ.copy()
    env["LOCAL_READER_STOP_ROOT"] = str(APP_DIR)
    env["LOCAL_READER_STOP_MARKERS"] = "|".join(APP_PROCESS_MARKERS)
    script = r"""
$root = $env:LOCAL_READER_STOP_ROOT
$markers = $env:LOCAL_READER_STOP_MARKERS -split '\|'
Get-CimInstance Win32_Process | Where-Object {
  $command = [string]$_.CommandLine
  if (-not $command -or $command.IndexOf($root, [StringComparison]::OrdinalIgnoreCase) -lt 0) { return $false }
  foreach ($marker in $markers) {
    if ($command.IndexOf($marker, [StringComparison]::OrdinalIgnoreCase) -ge 0) { return $true }
  }
  return $false
} | ForEach-Object { [string]$_.ProcessId }
"""
    try:
        output = subprocess.check_output(
            [
                "powershell",
                "-NoProfile",
                "-ExecutionPolicy",
                "Bypass",
                "-Command",
                script,
            ],
            env=env,
            text=True,
            errors="ignore",
            creationflags=CREATE_NO_WINDOW,
            timeout=15,
        )
    except Exception:
        return set()

    result = set()
    for line in output.splitlines():
        try:
            result.add(int(line.strip()))
        except ValueError:
            pass
    return result


def stop_pid(pid):
    subprocess.run(
        ["taskkill", "/PID", str(pid), "/T", "/F"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        creationflags=CREATE_NO_WINDOW,
        check=False,
    )


def stop_edge_profile():
    if os.name != "nt":
        return 0
    env = os.environ.copy()
    env["LOCAL_READER_EDGE_PROFILE"] = str(EDGE_PROFILE)
    script = r"""
$profile = $env:LOCAL_READER_EDGE_PROFILE
$items = Get-CimInstance Win32_Process | Where-Object {
  $_.Name -eq 'msedge.exe' -and
  $_.CommandLine -and
  ([string]$_.CommandLine).IndexOf($profile, [StringComparison]::OrdinalIgnoreCase) -ge 0
}
$count = @($items).Count
$items | ForEach-Object {
  try { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue } catch {}
}
[string]$count
"""
    try:
        output = subprocess.check_output(
            [
                "powershell",
                "-NoProfile",
                "-ExecutionPolicy",
                "Bypass",
                "-Command",
                script,
            ],
            env=env,
            text=True,
            errors="ignore",
            creationflags=CREATE_NO_WINDOW,
            timeout=15,
        )
        return int(output.strip().splitlines()[-1]) if output.strip() else 0
    except Exception:
        return 0


def main():
    stopped = set()
    known_app_pids = app_process_pids()

    # Port ownership is only used as a filter. A listener is never terminated
    # unless its command line also identifies this LocalReader installation.
    for port in PORTS:
        for pid in pids_on_port(port):
            if pid in known_app_pids and pid not in stopped:
                stop_pid(pid)
                stopped.add(pid)

    # Stop recognized app workers that are still starting or no longer listening.
    for pid in sorted(known_app_pids - stopped):
        stop_pid(pid)
        stopped.add(pid)

    edge_count = stop_edge_profile()
    print(
        f"Local Reader stopped "
        f"({len(stopped)} app processes, {edge_count} Edge processes)."
    )


if __name__ == "__main__":
    main()
