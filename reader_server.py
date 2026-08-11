import asyncio
import base64
import binascii
import copy
import hashlib
import gzip
import io
import json
import mimetypes
import os
import re
import secrets
import subprocess
import sys
import threading
import time
import traceback
import uuid
import wave
import zipfile
from array import array
from contextlib import contextmanager
from concurrent.futures import ThreadPoolExecutor, as_completed
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import shutil
from urllib.parse import parse_qs, urlparse
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from xml.sax.saxutils import escape as xml_escape

import pypdfium2 as pdfium
import pytesseract
from PIL import Image, ImageOps
import hmac
import ipaddress
from datetime import datetime, timezone
from urllib.parse import quote


# The public local-first build never binds to a LAN interface. A future
# network-enabled distribution should add its own authentication and threat
# model rather than turning this value into an environment-only switch.
BIND_HOST = "127.0.0.1"
HOST = "127.0.0.1"
PORT = 8765
APP_BUILD_ID = "localreader-v0.1.0-dev"
WORKER_PROTOCOL_ID = "localreader-vieneu-v3turbo-2"
MAX_REQUEST_BYTES = 384 * 1024 * 1024
MAX_DOCX_INPUT_BYTES = 256 * 1024 * 1024
MAX_DOCX_XML_BYTES = 64 * 1024 * 1024
MAX_PDF_INPUT_BYTES = 256 * 1024 * 1024
MAX_OCR_PAGES = 300
MAX_OCR_PIXELS = 25_000_000
ROOT = Path(__file__).resolve().parent
SHARED_AUDIO_DIR = (ROOT / "reader_audio_cache").resolve()
PROJECT_STORE = ROOT / "reader_project_store.json"
PROJECT_DEVICE_DIR = ROOT / "reader_device_stores"
PROJECT_PATCH_DIR = ROOT / "reader_device_patches"
R2_CONFIG_FILE = ROOT / "r2_config.json"
SUPABASE_CONFIG_FILE = ROOT / "supabase_config.disabled.json"
CLOUD_INDEX_FILE = ROOT / "cloud_sync_index.json"
R2_CLOUD_INDEX_FILE = ROOT / "r2_cloud_sync_index.json"
R2_DEVICE_INDEX_DIR = ROOT / "reader_r2_device_indexes"
R2_DEVICE_REMOVAL_DIR = ROOT / "reader_r2_device_removals"
CLOUD_DELETED_DOC_IDS_FILE = ROOT / "cloud_deleted_doc_ids.json"
DEVICE_DELETION_DIR = ROOT / "reader_device_deletions"
CLOUD_STORAGE_LIMIT_BYTES = 50 * 1024 * 1024 * 1024
SUPABASE_CLOUD_LIMIT_BYTES = 1 * 1024 * 1024 * 1024
R2_DEFAULT_CLOUD_LIMIT_BYTES = CLOUD_STORAGE_LIMIT_BYTES
LOCALAPPDATA = os.environ.get("LOCALAPPDATA", "")
RUNTIME_ROOT = Path(os.environ.get("LOCAL_READER_RUNTIME_DIR") or (Path(LOCALAPPDATA) / "LocalReaderApp" if LOCALAPPDATA else ROOT / ".runtime")).resolve()
AUDIO_DIR = Path(os.environ.get("LOCAL_READER_AUDIO_DIR") or (RUNTIME_ROOT / "reader_audio_cache")).resolve()
LOCAL_R2_CONFIG_FILE = RUNTIME_ROOT / "r2_config.json"
DEVICE_PROGRESS_DIR = ROOT / "reader_device_progress"
DEVICE_ID_FILE = RUNTIME_ROOT / "device_id.txt"
R2_PLAYER_ASSET_STATE_FILE = RUNTIME_ROOT / "r2_player_assets_state.json"
R2_AUTHORITATIVE_INDEX_FILE = RUNTIME_ROOT / "r2_authoritative_index.json"
TESSDATA_DIR = Path(os.environ.get("LOCAL_READER_TESSDATA_DIR") or (ROOT / "tessdata"))
TESSERACT_EXE = Path(os.environ.get("LOCAL_READER_TESSERACT_EXE") or shutil.which("tesseract") or r"C:\Program Files\Tesseract-OCR\tesseract.exe")
LOCAL_HF_HOME = Path(os.environ.get("LOCAL_READER_HF_HOME") or ((ROOT / ".hf_cache") if (ROOT / ".hf_cache").exists() else (RUNTIME_ROOT / "hf_cache")))


def set_path_hidden(path, hidden=True):
    """Keep OneDrive helper files hidden without touching their contents."""
    if os.name != "nt":
        return
    try:
        current_hidden = bool(getattr(path.stat(), "st_file_attributes", 0) & 0x2)
        if current_hidden == bool(hidden):
            return
        subprocess.run(
            ["attrib.exe", "+H" if hidden else "-H", str(path)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=subprocess.CREATE_NO_WINDOW,
            timeout=5,
            check=False,
        )
    except Exception:
        pass


def parse_ports(value, default):
    ports = []
    for raw in str(value or "").split(","):
        raw = raw.strip()
        if not raw:
            continue
        try:
            port = int(raw)
            if 1 <= port <= 65535 and port not in ports:
                ports.append(port)
        except ValueError:
            pass
    return ports or list(default)


def env_int(name, default, minimum=1, maximum=16):
    try:
        value = int(os.environ.get(name, "") or default)
    except ValueError:
        value = default
    return max(minimum, min(maximum, value))


def env_flag(name, default=True):
    raw = os.environ.get(name)
    if raw is None or str(raw).strip() == "":
        return bool(default)
    return str(raw).strip().lower() not in ("0", "false", "no", "off")


CLOUD_ENABLED = env_flag("LOCAL_READER_CLOUD_ENABLED", False)


def has_nvidia_gpu():
    if not shutil.which("nvidia-smi"):
        return False
    try:
        result = subprocess.run(
            ["nvidia-smi"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=5,
        )
        return result.returncode == 0
    except Exception:
        return False


def python_candidates():
    configured = os.environ.get("LOCAL_READER_VIENEU_PYTHON", "").strip()
    candidates = [
        Path(configured) if configured else None,
        ROOT / "python" / "python.exe",
        ROOT / ".vieneu_test" / "Scripts" / "python.exe",
        ROOT.parent / ".vieneu_test" / "Scripts" / "python.exe",
        RUNTIME_ROOT / ".vieneu_test" / "Scripts" / "python.exe",
        RUNTIME_ROOT / "venv" / "Scripts" / "python.exe",
        Path.home() / "anaconda3" / "envs" / "localreader" / "python.exe",
        Path.home() / "anaconda3" / "python.exe",
        Path.home() / "AppData" / "Local" / "Programs" / "Python" / "Python312" / "python.exe",
        Path.home() / "AppData" / "Local" / "Programs" / "Python" / "Python311" / "python.exe",
        Path.home() / "AppData" / "Local" / "Programs" / "Python" / "Python310" / "python.exe",
        Path(sys.executable),
    ]
    seen = set()
    for candidate in candidates:
        if not candidate:
            continue
        path = candidate.resolve()
        key = str(path).lower()
        if key in seen:
            continue
        seen.add(key)
        yield path


VIENEU_PORTS = parse_ports(os.environ.get("LOCAL_READER_VIENEU_PORTS"), [8766])
VIENEU_PORT = VIENEU_PORTS[0]
VIENEU_PYTHON = next((candidate for candidate in python_candidates() if candidate.exists()), RUNTIME_ROOT / ".vieneu_test" / "Scripts" / "python.exe")
VIENEU_WORKER = ROOT / "vieneu_worker.py"
VIENEU_SITE_PACKAGES = None
VIENEU_ENABLED = env_flag("LOCAL_READER_VIENEU_ENABLED", True)
NVIDIA_GPU_AVAILABLE = has_nvidia_gpu()
if os.environ.get("LOCAL_READER_TTS_MODEL_VERSION"):
    TTS_ENGINE_VERSION = os.environ["LOCAL_READER_TTS_MODEL_VERSION"]
else:
    TTS_ENGINE_VERSION = "vieneu-tts-v3-turbo-48khz"

VIENEU_DEFAULT_VOICE = (os.environ.get("LOCAL_READER_VIENEU_VOICE") or "Trúc Ly").strip() or "Trúc Ly"
VIENEU_DEFAULT_VOICE_LABEL = (
    os.environ.get("LOCAL_READER_VIENEU_VOICE_LABEL")
    or f"{VIENEU_DEFAULT_VOICE} [VieNeu v3]"
).strip()


def active_tts_audio_voice():
    return VIENEU_DEFAULT_VOICE


def active_tts_audio_voice_label():
    return VIENEU_DEFAULT_VOICE_LABEL


def active_tts_audio_engine():
    return "vieneu"


def active_tts_job_metadata():
    return {
        "audioVoice": active_tts_audio_voice(),
        "audioVoiceLabel": active_tts_audio_voice_label(),
        "audioEngine": active_tts_audio_engine(),
        "audioModelVersion": TTS_ENGINE_VERSION,
    }


def active_tts_engine_label():
    if VIENEU_ENABLED:
        return "vieneu"
    return "disabled"


def doc_audio_voice_value(doc, default=None):
    if not isinstance(doc, dict):
        return default or VIENEU_DEFAULT_VOICE
    candidates = [doc.get("audioVoice")]
    for field in ("audioJobs", "audioItems"):
        records = doc.get(field) if isinstance(doc.get(field), list) else []
        candidates.extend(item.get("voice") for item in records if isinstance(item, dict))
    manifest = doc_audio_manifest(doc) if "doc_audio_manifest" in globals() else {}
    for key in manifest.keys():
        parts = str(key or "").split("|")
        if len(parts) >= 4 and parts[0] == "vieneu":
            candidates.append(parts[3])
    for value in candidates:
        voice = str(value or "").strip()
        if voice:
            return voice
    return default or VIENEU_DEFAULT_VOICE


def doc_audio_voice_label_value(doc, voice=None, default=None):
    if isinstance(doc, dict):
        label = str(doc.get("audioVoiceLabel") or "").strip()
        if label:
            return label
    voice = voice or doc_audio_voice_value(doc, default=default)
    if voice == VIENEU_DEFAULT_VOICE:
        return VIENEU_DEFAULT_VOICE_LABEL
    if voice == "Ly":
        return "Ly [VieNeu]"
    return f"{voice} [VieNeu]"

LOCAL_HF_HOME.mkdir(parents=True, exist_ok=True)
RUNTIME_ROOT.mkdir(parents=True, exist_ok=True)
DEVICE_PROGRESS_DIR.mkdir(parents=True, exist_ok=True)
PROJECT_DEVICE_DIR.mkdir(parents=True, exist_ok=True)
R2_DEVICE_INDEX_DIR.mkdir(parents=True, exist_ok=True)
DEVICE_DELETION_DIR.mkdir(parents=True, exist_ok=True)
os.environ.setdefault("HF_HOME", str(LOCAL_HF_HOME))
os.environ.setdefault("HUGGINGFACE_HUB_CACHE", str(LOCAL_HF_HOME / "hub"))
os.environ.setdefault("TRANSFORMERS_CACHE", str(LOCAL_HF_HOME / "hub"))

if VIENEU_PYTHON.exists():
    venv_site = VIENEU_PYTHON.parent.parent / "Lib" / "site-packages"
    if venv_site.exists():
        VIENEU_SITE_PACKAGES = venv_site
if not TESSDATA_DIR.exists():
    for candidate in (
        ROOT.parent / "tessdata",
        Path(r"C:\Program Files\Tesseract-OCR\tessdata"),
        Path(r"C:\Program Files (x86)\Tesseract-OCR\tessdata"),
        Path.home() / "Documents" / "New project" / "tessdata",
    ):
        if candidate.exists():
            TESSDATA_DIR = candidate
            break

AUDIO_DIR.mkdir(parents=True, exist_ok=True)

if TESSERACT_EXE.exists():
    pytesseract.pytesseract.tesseract_cmd = str(TESSERACT_EXE)
if TESSDATA_DIR.exists():
    os.environ["TESSDATA_PREFIX"] = str(TESSDATA_DIR)

VOICE_CACHE = None
VOICE_LOCK = asyncio.Lock()
VIENEU_PROCESSES = {}
VIENEU_LOCK = threading.Lock()
VIENEU_READY_PORTS = []
VIENEU_READY_LOCK = threading.Lock()
VIENEU_WORKER_SLOTS = {port: threading.Lock() for port in VIENEU_PORTS}
VIENEU_ASSIGN_LOCK = threading.Lock()
VIENEU_ASSIGN_CURSOR = 0
VIENEU_WORKER_TOKEN = (os.environ.get("LOCAL_READER_WORKER_TOKEN") or secrets.token_urlsafe(32)).strip()
SERVER_SHUTDOWN_EVENT = threading.Event()
PROJECT_LOCK = threading.RLock()
PROGRESS_LOCK = threading.RLock()
DELETION_LOCK = threading.RLock()
R2_INDEX_CACHE_LOCK = threading.RLock()
R2_ASSET_UPLOAD_LOCK = threading.Lock()
BACKGROUND_LOCK = threading.Lock()
BACKGROUND_JOBS = {}
BACKGROUND_MAX_WORKERS = env_int("LOCAL_READER_BACKGROUND_WORKERS", len(VIENEU_PORTS), 1, 8)
BACKGROUND_PROJECT_SEMAPHORE = threading.Semaphore(1)
AUTO_PROJECT_CHAIN_LOCK = threading.Lock()
AUTO_PROJECT_CHAIN_IN_FLIGHT = False
AUTO_PROJECT_CHAIN_STARTED_TS = 0.0
CLOUD_AUTO_SYNC_LOCK = threading.Lock()
CLOUD_AUTO_SYNC_IN_FLIGHT = set()
CLOUD_SYNC_SERIAL_LOCK = threading.RLock()
CLOUD_LIBRARY_REBUILD_LOCK = threading.Lock()
CLOUD_LIBRARY_REBUILD_QUEUED = False
CLOUD_DELETE_RETRY_LOCK = threading.Lock()
CLOUD_DELETE_RETRY_IDS = set()
CLOUD_DELETE_RETRY_RUNNING = False
R2_RESET_CLEANUP_LOCK = threading.Lock()
R2_RESET_CLEANUP_QUEUED = False
R2_ORPHAN_CLEANUP_LOCK = threading.Lock()
R2_ORPHAN_CLEANUP_QUEUED = False
PROJECT_CACHE_SIGNATURE = None
PROJECT_CACHE_STORE = None
# Keep parsed snapshots by file signature.  The shared library contains several
# 30 MB device stores; rebuilding the merged store should not re-read and parse
# every unchanged JSON file whenever audio progress touches one store.
PROJECT_SNAPSHOT_CACHE = {}
DELETION_CACHE_SIGNATURE = None
DELETION_CACHE_IDS = frozenset()
R2_INDEX_CACHE_SIGNATURE = None
R2_INDEX_CACHE_DATA = None
R2_INDEX_CACHE_TOTAL_BYTES = 0
R2_REMOTE_AUDIO_SOURCE_ID = None
R2_REMOTE_AUDIO_MAP = {}
R2_AUTHORITY_REFRESH_LOCK = threading.Lock()
R2_AUTHORITY_REFRESHED_AT = 0.0
REMOTE_AUDIO_DOWNLOAD_LOCK = threading.Lock()
REMOTE_AUDIO_DOWNLOAD_LOCKS = {}
ACTIVE_R2_LEASE_LOCK = threading.RLock()
ACTIVE_R2_LEASE = None
VIENEU_LEGACY_MODEL_VERSION = "vieneu-tts-v2-neucodec-int8"
VIENEU_MODEL_VERSION = os.environ.get("LOCAL_READER_VIENEU_MODEL_VERSION") or TTS_ENGINE_VERSION
ALLOW_LEGACY_VIENEU_CACHE = VIENEU_MODEL_VERSION == VIENEU_LEGACY_MODEL_VERSION
VIENEU_AUDIO_SPEED = "1.0x"
AUTO_PROJECT_WATCHDOG_ENABLED = env_flag("LOCAL_READER_AUTO_PROJECT_WATCHDOG", NVIDIA_GPU_AVAILABLE)
AUTO_PROJECT_WATCHDOG_SECONDS = env_int("LOCAL_READER_AUTO_PROJECT_WATCHDOG_SECONDS", 45, 10, 600)
AUDIO_POSTPROCESS_VERSION = "clean-softlimit-v1"
AUDIO_POSTPROCESS_ENABLED = env_flag("LOCAL_READER_AUDIO_POSTPROCESS_ENABLED", True)
AUDIO_POSTPROCESS_TARGET_PEAK = 0.84
AUDIO_POSTPROCESS_TARGET_RMS = 0.13



def make_json(data, status=200):
    body = json.dumps(data, ensure_ascii=False).encode("utf-8")
    return status, body, "application/json; charset=utf-8"


def read_json_bytes(raw):
    for encoding in ("utf-8-sig", "utf-8", "cp1258", "mbcs"):
        try:
            return json.loads(raw.decode(encoding))
        except UnicodeDecodeError:
            continue
    return json.loads(raw.decode("utf-8", errors="replace"))


def file_set_signature(paths):
    signature = []
    for path in sorted({Path(item) for item in paths}, key=lambda item: str(item).casefold()):
        try:
            stat = path.stat()
            signature.append((str(path), stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns))
        except OSError:
            signature.append((str(path), -1, -1, -1))
    return tuple(signature)


def atomic_write_text(path, text, encoding="utf-8", retries=10):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.stem}.{os.getpid()}.{threading.get_ident()}.{time.time_ns()}.tmp")
    try:
        tmp.write_text(text, encoding=encoding)
        last_error = None
        for attempt in range(max(1, int(retries or 1))):
            try:
                tmp.replace(path)
                return path
            except PermissionError as exc:
                last_error = exc
                time.sleep(min(1.5, 0.12 * (attempt + 1)))
        if last_error:
            raise last_error
        raise OSError(f"Could not replace {path}")
    finally:
        try:
            tmp.unlink(missing_ok=True)
        except Exception:
            pass


def safe_device_id(value):
    cleaned = re.sub(r"[^A-Za-z0-9_-]+", "-", str(value or "").strip()).strip("-_")
    return cleaned[:80]


def current_device_id():
    with PROGRESS_LOCK:
        try:
            existing = safe_device_id(DEVICE_ID_FILE.read_text(encoding="utf-8"))
            if existing:
                return existing
        except Exception:
            pass
        machine = safe_device_id(os.environ.get("COMPUTERNAME") or "device") or "device"
        device_id = f"{machine}-{uuid.uuid4().hex[:12]}"
        DEVICE_ID_FILE.parent.mkdir(parents=True, exist_ok=True)
        tmp = DEVICE_ID_FILE.with_name(f"{DEVICE_ID_FILE.name}.{os.getpid()}.{time.time_ns()}.tmp")
        try:
            tmp.write_text(device_id, encoding="utf-8")
            tmp.replace(DEVICE_ID_FILE)
        finally:
            try:
                tmp.unlink(missing_ok=True)
            except Exception:
                pass
        return device_id


def iso_timestamp(value):
    raw = str(value or "").strip()
    if not raw:
        return 0.0
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=datetime.now().astimezone().tzinfo)
        return parsed.timestamp()
    except Exception:
        return 0.0


def bounded_client_timestamp(value):
    timestamp = iso_timestamp(value)
    if not timestamp or timestamp > time.time() + 600:
        return 0.0
    return timestamp


def normalize_project_doc_timestamps(doc):
    if not isinstance(doc, dict):
        return doc
    now = time.time()
    now_iso = datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")
    for key in (
        "contentEditedAt",
        "layoutUpdatedAt",
        "updatedAt",
        "createdAt",
        "audioUpdatedAt",
        "audioPreparedAt",
        "audioResetAt",
    ):
        value = doc.get(key)
        if value and iso_timestamp(value) > now + 600:
            doc[key] = now_iso
    return doc


def progress_timestamp(record):
    if not isinstance(record, dict):
        return 0.0
    return bounded_client_timestamp(record.get("progressUpdatedAt")) or bounded_client_timestamp(record.get("lastReadAt"))


def copy_progress_fields(target, source):
    if not isinstance(target, dict) or not isinstance(source, dict):
        return target
    for key in ("currentIndex", "currentPartIndex", "lastReadAt", "progressUpdatedAt"):
        if key in source:
            target[key] = source.get(key)
    return target


def read_all_device_progress():
    latest = {}
    try:
        paths = list(DEVICE_PROGRESS_DIR.glob("*.json"))
    except Exception:
        paths = []
    for path in paths:
        try:
            data = json.loads(path.read_text(encoding="utf-8-sig"))
        except Exception:
            continue
        records = data.get("docs") if isinstance(data, dict) else None
        if isinstance(records, list):
            records = {str(item.get("docId") or item.get("id") or ""): item for item in records if isinstance(item, dict)}
        if not isinstance(records, dict):
            continue
        for doc_id, record in records.items():
            doc_id = str(doc_id or "").strip()
            if not doc_id or not isinstance(record, dict):
                continue
            candidate = dict(record)
            candidate["docId"] = doc_id
            current = latest.get(doc_id)
            if current is None or progress_timestamp(candidate) >= progress_timestamp(current):
                latest[doc_id] = candidate
    return latest


def overlay_device_progress(store):
    if not isinstance(store, dict):
        return store
    latest = read_all_device_progress()
    if not latest:
        return store
    docs = store.get("docs") if isinstance(store.get("docs"), list) else []
    merged_docs = []
    for item in docs:
        if not isinstance(item, dict):
            continue
        doc = dict(item)
        progress = latest.get(str(doc.get("id") or "").strip())
        if progress and progress_timestamp(progress) >= progress_timestamp(doc):
            copy_progress_fields(doc, progress)
        merged_docs.append(doc)
    result = dict(store)
    result["docs"] = merged_docs
    return result


def write_device_progress(payload):
    doc_id = str(payload.get("docId") or payload.get("id") or "").strip()
    if not doc_id:
        raise ValueError("Progress doc id is required")
    now_iso = datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")
    try:
        current_index = max(0, int(payload.get("currentIndex") or 0))
    except Exception:
        current_index = 0
    try:
        current_part_index = max(0, int(payload.get("currentPartIndex") or 0))
    except Exception:
        current_part_index = 0
    client_progress_at = str(payload.get("progressUpdatedAt") or "").strip()
    client_progress_ts = iso_timestamp(client_progress_at)
    if not client_progress_ts or client_progress_ts > time.time() + 600:
        client_progress_at = now_iso
    record = {
        "docId": doc_id,
        "currentIndex": current_index,
        "currentPartIndex": current_part_index,
        "lastReadAt": str(payload.get("lastReadAt") or "").strip(),
        "progressUpdatedAt": client_progress_at,
    }
    device_id = current_device_id()
    path = DEVICE_PROGRESS_DIR / f"{device_id}.json"
    with PROGRESS_LOCK:
        try:
            data = json.loads(path.read_text(encoding="utf-8-sig")) if path.exists() else {}
        except Exception:
            data = {}
        docs = data.get("docs") if isinstance(data.get("docs"), dict) else {}
        existing_record = docs.get(doc_id) if isinstance(docs.get(doc_id), dict) else None
        if existing_record and progress_timestamp(existing_record) > progress_timestamp(record):
            record = existing_record
        else:
            docs[doc_id] = record
        data = {
            "version": 1,
            "deviceId": device_id,
            "deviceName": os.environ.get("COMPUTERNAME") or "device",
            "savedAt": now_iso,
            "docs": docs,
        }
        DEVICE_PROGRESS_DIR.mkdir(parents=True, exist_ok=True)
        atomic_write_text(path, json.dumps(data, ensure_ascii=False, indent=2))
        if os.name == "nt":
            try:
                subprocess.run(["attrib", "+h", str(DEVICE_PROGRESS_DIR)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=3)
                subprocess.run(["attrib", "+h", str(path)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=3)
            except Exception:
                pass
    return {"ok": True, "deviceId": device_id, "progress": record, "savedAt": now_iso}


def project_device_store_path():
    return PROJECT_DEVICE_DIR / f"{current_device_id()}.json"


def project_doc_timestamp(doc):
    if not isinstance(doc, dict):
        return 0.0
    value = max(
        bounded_client_timestamp(doc.get("contentEditedAt")),
        bounded_client_timestamp(doc.get("audioUpdatedAt")),
        bounded_client_timestamp(doc.get("audioPreparedAt")),
        bounded_client_timestamp(doc.get("audioResetAt")),
        bounded_client_timestamp(doc.get("updatedAt")),
        bounded_client_timestamp(doc.get("createdAt")),
    )
    return value or 0.0


def project_snapshot_paths():
    sources = []
    if PROJECT_STORE.exists():
        sources.append(PROJECT_STORE)
    try:
        sources.extend(sorted(PROJECT_DEVICE_DIR.glob("*.json")))
    except Exception:
        pass
    try:
        sources.extend(sorted(PROJECT_PATCH_DIR.glob("*/*.json")))
    except Exception:
        pass
    return sources


def read_project_snapshots(sources=None):
    sources = list(sources) if sources is not None else project_snapshot_paths()
    snapshots = []
    active_keys = set()
    for source in sources:
        source = Path(source)
        source_key = str(source)
        try:
            stat = source.stat()
            signature = (stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns)
        except OSError:
            continue
        active_keys.add(source_key)
        cached = PROJECT_SNAPSHOT_CACHE.get(source_key)
        if cached and cached[0] == signature:
            snapshot = cached[1]
        else:
            snapshot = None
            try:
                data = json.loads(source.read_text(encoding="utf-8-sig"))
                if isinstance(data, dict) and isinstance(data.get("docs"), list):
                    snapshot = {
                        "path": source,
                        "data": data,
                        "savedAt": str(data.get("savedAt") or ""),
                        "savedTs": bounded_client_timestamp(data.get("savedAt")),
                    }
            except Exception:
                snapshot = None
            PROJECT_SNAPSHOT_CACHE[source_key] = (signature, snapshot)
        if snapshot is not None:
            snapshots.append(snapshot)
    for stale_key in set(PROJECT_SNAPSHOT_CACHE) - active_keys:
        PROJECT_SNAPSHOT_CACHE.pop(stale_key, None)
    return snapshots


def merge_project_snapshots(snapshots):
    if not snapshots:
        return {"docs": [], "activeDocId": "", "savedAt": ""}
    ordered_snapshots = sorted(snapshots, key=lambda item: (item.get("savedTs") or 0, str(item.get("path") or "")))
    newest = ordered_snapshots[-1]
    candidates = {}
    deleted_ids = read_cloud_deleted_doc_ids()
    for snapshot in ordered_snapshots:
        snapshot_ts = snapshot.get("savedTs") or 0
        is_delta_snapshot = str(snapshot["data"].get("kind") or "") == "project-doc-delta"
        for raw_doc in snapshot["data"].get("docs") or []:
            if not isinstance(raw_doc, dict):
                continue
            doc_id = str(raw_doc.get("id") or "").strip()
            if not doc_id or doc_id in deleted_ids:
                continue
            candidates.setdefault(doc_id, []).append((project_doc_timestamp(raw_doc), snapshot_ts, raw_doc, is_delta_snapshot))
    docs = []
    for doc_id, records in candidates.items():
        records.sort(key=lambda row: (row[0], row[1]))
        chosen = normalize_project_doc_timestamps(dict(records[-1][2]))
        layout_record = max(
            records,
            key=lambda row: (
                bounded_client_timestamp(row[2].get("layoutUpdatedAt"))
                or (0 if row[3] else row[1]),
                row[1],
            ),
        )
        layout_source = layout_record[2]
        for field in ("desktopOrder", "folderPath", "collection", "layoutUpdatedAt"):
            if field in layout_source:
                chosen[field] = layout_source.get(field)
        # Audio-only deltas may be newer than a text edit from another machine.
        # Keep content fields from the record with the newest content timestamp
        # so a late audio flush can never roll back document text.
        content_record = max(
            records,
            key=lambda row: (
                bounded_client_timestamp(row[2].get("contentEditedAt"))
                or (0 if row[3] else row[1]),
                row[1],
            ),
        )
        content_source = content_record[2]
        for field in (
            "title",
            "text",
            "sourceName",
            "chunkProfile",
            "chunkCount",
            "richText",
            "richHtml",
            "richChunks",
            "chunks",
            "contentEditedAt",
        ):
            if field in content_source:
                chosen[field] = content_source.get(field)
        merged_manifest = {}
        newest_reset = max(bounded_client_timestamp(row[2].get("audioResetAt")) for row in records)
        for _doc_ts, _snapshot_ts, record, _is_delta in records:
            if newest_reset and bounded_client_timestamp(record.get("audioResetAt")) < newest_reset:
                continue
            manifest = record.get("audioManifest") if isinstance(record.get("audioManifest"), dict) else {}
            merged_manifest.update(manifest)
        if merged_manifest:
            chosen["audioManifest"] = merged_manifest
        docs.append(chosen)
    docs.sort(key=lambda doc: (
        int(doc.get("desktopOrder")) if str(doc.get("desktopOrder", "")).lstrip("-").isdigit() else 1000000,
        str(doc.get("id") or ""),
    ))
    return {
        "docs": docs,
        "activeDocId": newest["data"].get("activeDocId") or (docs[0].get("id") if docs else ""),
        "savedAt": newest.get("savedAt") or "",
    }


def read_project_store():
    global PROJECT_CACHE_SIGNATURE, PROJECT_CACHE_STORE
    with PROJECT_LOCK:
        sources = project_snapshot_paths()
        signature = (
            file_set_signature(sources),
            file_set_signature(deleted_doc_source_paths()),
        )
        if PROJECT_CACHE_STORE is None or signature != PROJECT_CACHE_SIGNATURE:
            PROJECT_CACHE_STORE = merge_project_snapshots(read_project_snapshots(sources))
            PROJECT_CACHE_SIGNATURE = signature
        return overlay_device_progress(copy.deepcopy(PROJECT_CACHE_STORE))


PROJECT_BOOT_HEAVY_FIELDS = {
    "text",
    "richText",
    "richHtml",
    "richChunks",
    "chunks",
    "audioItems",
    "audioJobs",
}


def project_doc_shell(doc):
    """Return list/card metadata without copying the large document body."""
    if not isinstance(doc, dict):
        return {}
    shell = {
        key: value
        for key, value in doc.items()
        if key not in PROJECT_BOOT_HEAVY_FIELDS
    }
    source_text = str(doc.get("text") or "")
    shell["text"] = ""
    shell["textAvailable"] = bool(source_text.strip())
    shell["textLength"] = len(source_text)
    shell["serverBacked"] = True
    return shell


def project_boot_store(requested_doc_id=""):
    """Small startup payload plus one fully hydrated active document."""
    store = read_project_store()
    docs = store.get("docs") if isinstance(store.get("docs"), list) else []
    requested_doc_id = str(requested_doc_id or "").strip()
    active_doc_id = requested_doc_id or str(store.get("activeDocId") or "").strip()
    active_doc = next((doc for doc in docs if str(doc.get("id") or "") == active_doc_id), None)
    if active_doc is None and docs:
        active_doc = docs[0]
        active_doc_id = str(active_doc.get("id") or "")
    return {
        "docs": [project_doc_shell(doc) for doc in docs if isinstance(doc, dict)],
        "activeDocId": active_doc_id,
        "activeDoc": active_doc or None,
        "savedAt": store.get("savedAt") or "",
    }


def write_project_store(payload):
    global PROJECT_CACHE_SIGNATURE, PROJECT_CACHE_STORE
    with PROJECT_LOCK:
        deleted_doc_ids = clean_doc_ids(payload.get("deletedDocIds") if isinstance(payload.get("deletedDocIds"), list) else [])
        if deleted_doc_ids:
            remember_cloud_deleted_doc_ids(deleted_doc_ids)
            with BACKGROUND_LOCK:
                for deleted_doc_id in deleted_doc_ids:
                    BACKGROUND_JOBS.pop(deleted_doc_id, None)
        device_id = current_device_id()
        target = project_device_store_path()
        data = {
            "version": 2,
            "deviceId": device_id,
            "deviceName": os.environ.get("COMPUTERNAME") or "device",
            "docs": payload.get("docs") if isinstance(payload.get("docs"), list) else [],
            "activeDocId": payload.get("activeDocId") or "",
            "savedAt": datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z"),
            "deletedDocIds": deleted_doc_ids,
        }
        data = sanitize_project_store_for_write(data)
        data = overlay_device_progress(data)
        data.pop("deletedDocIds", None)
        PROJECT_DEVICE_DIR.mkdir(parents=True, exist_ok=True)
        atomic_write_text(target, json.dumps(data, ensure_ascii=False, indent=2))
        PROJECT_CACHE_SIGNATURE = None
        PROJECT_CACHE_STORE = None
        if os.name == "nt":
            try:
                subprocess.run(["attrib", "+h", str(PROJECT_DEVICE_DIR)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=3)
                subprocess.run(["attrib", "+h", str(target)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=3)
            except Exception:
                pass
        try:
            schedule_reset_cloud_cleanup_background(data, delay=1.2)
        except Exception:
            pass
        return data


def audio_url(filename):
    return f"/audio/{filename}"


def resolve_audio_path(filename, require_existing=True):
    """Read local runtime audio first, then the legacy shared OneDrive cache."""
    name = Path(str(filename or "")).name
    if not name:
        return None if require_existing else AUDIO_DIR
    candidates = [AUDIO_DIR / name]
    if SHARED_AUDIO_DIR != AUDIO_DIR:
        candidates.append(SHARED_AUDIO_DIR / name)
    for candidate in candidates:
        try:
            if candidate.is_file() and candidate.stat().st_size > 0:
                return candidate
        except OSError:
            continue
    return None if require_existing else candidates[0]


def load_r2_config():
    config = {}
    def read_candidate(path):
        try:
            value = json.loads(path.read_text(encoding="utf-8-sig"))
            return value if isinstance(value, dict) else None
        except Exception:
            return None

    local_data = read_candidate(LOCAL_R2_CONFIG_FILE) if LOCAL_R2_CONFIG_FILE.exists() else None
    shared_data = read_candidate(R2_CONFIG_FILE) if R2_CONFIG_FILE.exists() else None
    local_mtime = LOCAL_R2_CONFIG_FILE.stat().st_mtime_ns if local_data is not None else -1
    shared_mtime = R2_CONFIG_FILE.stat().st_mtime_ns if shared_data is not None else -1
    if shared_data is not None and (local_data is None or shared_mtime > local_mtime):
        config.update(shared_data)
        try:
            atomic_write_text(LOCAL_R2_CONFIG_FILE, json.dumps(shared_data, ensure_ascii=False, indent=2))
            set_path_hidden(LOCAL_R2_CONFIG_FILE)
        except Exception:
            pass
    elif local_data is not None:
        config.update(local_data)
    elif shared_data is not None:
        config.update(shared_data)
    env_map = {
        "account_id": "R2_ACCOUNT_ID",
        "api_token": "R2_API_TOKEN",
        "access_key_id": "R2_ACCESS_KEY_ID",
        "secret_access_key": "R2_SECRET_ACCESS_KEY",
        "bucket": "R2_BUCKET",
        "public_base_url": "R2_PUBLIC_BASE_URL",
        "prefix": "R2_PREFIX",
        "endpoint": "R2_ENDPOINT",
        "max_storage_bytes": "R2_MAX_STORAGE_BYTES",
    }
    for key, env_name in env_map.items():
        if os.environ.get(env_name):
            config[key] = os.environ[env_name]
    config["prefix"] = str(config.get("prefix") or "local-reader").strip("/")
    return config


def r2_missing_fields(config):
    required = ["account_id", "bucket", "public_base_url"]
    missing = [key for key in required if not str(config.get(key) or "").strip()]
    has_s3_keys = bool(str(config.get("access_key_id") or "").strip() and str(config.get("secret_access_key") or "").strip())
    if not has_s3_keys:
        missing.extend(["access_key_id", "secret_access_key"])
    return missing


def r2_object_key(config, rel_path):
    rel = str(rel_path or "").replace("\\", "/").strip("/")
    prefix = str(config.get("prefix") or "").strip("/")
    return f"{prefix}/{rel}".strip("/") if prefix else rel


def r2_private_runtime_key(config, name):
    scope_material = "|".join([
        "localreader-v3-authority",
        str(config.get("account_id") or ""),
        str(config.get("bucket") or ""),
        str(config.get("prefix") or ""),
    ])
    scope = hashlib.sha256(scope_material.encode("utf-8")).hexdigest()[:24]
    return r2_object_key(config, f"_runtime/{scope}/{str(name or '').strip('/')}")


def r2_public_url(config, rel_path):
    return f"{str(config.get('public_base_url') or '').rstrip('/')}/{r2_object_key(config, rel_path)}"


def r2_cloud_limit_bytes(config=None):
    config = config or load_r2_config()
    try:
        value = int(float(config.get("max_storage_bytes") or 0))
        return value if value > 0 else R2_DEFAULT_CLOUD_LIMIT_BYTES
    except Exception:
        return R2_DEFAULT_CLOUD_LIMIT_BYTES


def r2_api_object_url(config, object_key):
    account_id = quote(str(config.get("account_id") or "").strip(), safe="")
    bucket = quote(str(config.get("bucket") or "").strip(), safe="")
    encoded_key = quote(str(object_key or ""), safe="")
    return f"https://api.cloudflare.com/client/v4/accounts/{account_id}/r2/buckets/{bucket}/objects/{encoded_key}"


def r2_api_put_object(config, object_key, data, content_type):
    token = str(config.get("api_token") or "").strip()
    if not token:
        raise RuntimeError("Missing R2 API token")
    req = urllib.request.Request(
        r2_api_object_url(config, object_key),
        data=data,
        method="PUT",
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": content_type or "application/octet-stream",
        },
    )
    with urllib.request.urlopen(req, timeout=180) as resp:
        raw = resp.read().decode("utf-8", errors="replace")
    try:
        payload = json.loads(raw) if raw else {}
    except Exception:
        payload = {}
    if payload and payload.get("success") is False:
        raise RuntimeError(f"R2 upload failed: {payload}")
    return payload


def r2_delete_object(config, object_key):
    token = str(config.get("api_token") or "").strip()
    if not token or not object_key:
        return False
    req = urllib.request.Request(
        r2_api_object_url(config, object_key),
        method="DELETE",
        headers={"Authorization": f"Bearer {token}"},
    )
    try:
        with urllib.request.urlopen(req, timeout=90) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
        payload = json.loads(raw) if raw else {}
        return bool(payload.get("success", True))
    except Exception:
        return False


def r2_base36(value):
    chars = "0123456789abcdefghijklmnopqrstuvwxyz"
    value = int(value) & 0xFFFFFFFF
    if value == 0:
        return "0"
    out = ""
    while value:
        value, rem = divmod(value, 36)
        out = chars[rem] + out
    return out


def r2_signing_key(secret_key, date_stamp):
    key = ("AWS4" + secret_key).encode("utf-8")
    for part in (date_stamp, "auto", "s3", "aws4_request"):
        key = hmac.new(key, part.encode("utf-8"), hashlib.sha256).digest()
    return key


def r2_put_object(config, object_key, data, content_type, conditional_headers=None, return_headers=False):
    assert_r2_cloud_lease_active()
    account_id = str(config.get("account_id") or "").strip()
    access_key = str(config.get("access_key_id") or "").strip()
    secret_key = str(config.get("secret_access_key") or "").strip()
    bucket = str(config.get("bucket") or "").strip()
    if not (account_id and access_key and secret_key and bucket):
        if conditional_headers:
            raise RuntimeError("Conditional R2 writes require S3 access keys")
        return r2_api_put_object(config, object_key, data, content_type)
    host = f"{account_id}.r2.cloudflarestorage.com"
    encoded_key = "/".join(quote(part, safe="") for part in str(object_key).split("/"))
    canonical_uri = f"/{quote(bucket, safe='')}/{encoded_key}"
    url = f"https://{host}{canonical_uri}"
    payload_hash = hashlib.sha256(data).hexdigest()
    now = datetime.now(timezone.utc)
    amz_date = now.strftime("%Y%m%dT%H%M%SZ")
    date_stamp = now.strftime("%Y%m%d")
    headers_lower = {
        "content-type": content_type,
        "host": host,
        "x-amz-content-sha256": payload_hash,
        "x-amz-date": amz_date,
    }
    for key, value in (conditional_headers or {}).items():
        headers_lower[str(key).strip().lower()] = str(value).strip()
    signed_headers = ";".join(sorted(headers_lower))
    canonical_headers = "".join(f"{key}:{headers_lower[key]}\n" for key in sorted(headers_lower))
    canonical_request = "\n".join([
        "PUT",
        canonical_uri,
        "",
        canonical_headers,
        signed_headers,
        payload_hash,
    ])
    scope = f"{date_stamp}/auto/s3/aws4_request"
    string_to_sign = "\n".join([
        "AWS4-HMAC-SHA256",
        amz_date,
        scope,
        hashlib.sha256(canonical_request.encode("utf-8")).hexdigest(),
    ])
    signature = hmac.new(r2_signing_key(secret_key, date_stamp), string_to_sign.encode("utf-8"), hashlib.sha256).hexdigest()
    headers = dict(headers_lower)
    headers["Authorization"] = f"AWS4-HMAC-SHA256 Credential={access_key}/{scope}, SignedHeaders={signed_headers}, Signature={signature}"
    request = urllib.request.Request(url, data=data, headers=headers, method="PUT")
    with urllib.request.urlopen(request, timeout=120) as response:
        if return_headers:
            return {"status": response.status, "etag": str(response.headers.get("ETag") or "")}
        return response.status


def r2_get_object_with_etag(config, object_key):
    account_id = str(config.get("account_id") or "").strip()
    access_key = str(config.get("access_key_id") or "").strip()
    secret_key = str(config.get("secret_access_key") or "").strip()
    bucket = str(config.get("bucket") or "").strip()
    if not (account_id and access_key and secret_key and bucket):
        raise RuntimeError("R2 lease reads require S3 access keys")
    host = f"{account_id}.r2.cloudflarestorage.com"
    encoded_key = "/".join(quote(part, safe="") for part in str(object_key).split("/"))
    canonical_uri = f"/{quote(bucket, safe='')}/{encoded_key}"
    url = f"https://{host}{canonical_uri}"
    payload_hash = hashlib.sha256(b"").hexdigest()
    now = datetime.now(timezone.utc)
    amz_date = now.strftime("%Y%m%dT%H%M%SZ")
    date_stamp = now.strftime("%Y%m%d")
    headers_lower = {
        "host": host,
        "x-amz-content-sha256": payload_hash,
        "x-amz-date": amz_date,
    }
    signed_headers = ";".join(sorted(headers_lower))
    canonical_headers = "".join(f"{key}:{headers_lower[key]}\n" for key in sorted(headers_lower))
    canonical_request = "\n".join(["GET", canonical_uri, "", canonical_headers, signed_headers, payload_hash])
    scope = f"{date_stamp}/auto/s3/aws4_request"
    string_to_sign = "\n".join([
        "AWS4-HMAC-SHA256", amz_date, scope,
        hashlib.sha256(canonical_request.encode("utf-8")).hexdigest(),
    ])
    signature = hmac.new(r2_signing_key(secret_key, date_stamp), string_to_sign.encode("utf-8"), hashlib.sha256).hexdigest()
    headers = dict(headers_lower)
    headers["Authorization"] = f"AWS4-HMAC-SHA256 Credential={access_key}/{scope}, SignedHeaders={signed_headers}, Signature={signature}"
    request = urllib.request.Request(url, headers=headers, method="GET")
    with urllib.request.urlopen(request, timeout=60) as response:
        return response.read(), str(response.headers.get("ETag") or "")


def refresh_r2_authoritative_index(config):
    object_key = r2_private_runtime_key(config, "cloud-index.json.gz")
    try:
        compressed, _etag = r2_get_object_with_etag(config, object_key)
        data = json.loads(gzip.decompress(compressed).decode("utf-8-sig"))
        if not isinstance(data, dict) or not isinstance(data.get("docs"), list) or data.get("runtimeKey") != object_key:
            raise ValueError("Invalid authoritative R2 index")
        atomic_write_text(R2_AUTHORITATIVE_INDEX_FILE, json.dumps(data, ensure_ascii=False, separators=(",", ":")))
        set_path_hidden(R2_AUTHORITATIVE_INDEX_FILE)
        return data
    except urllib.error.HTTPError as exc:
        if exc.code != 404:
            raise
    except FileNotFoundError:
        pass
    except (gzip.BadGzipFile, EOFError, UnicodeError, ValueError, json.JSONDecodeError):
        pass
    try:
        R2_AUTHORITATIVE_INDEX_FILE.unlink(missing_ok=True)
    except Exception:
        pass
    return None


def publish_r2_authoritative_index(config, index):
    assert_r2_cloud_lease_active()
    object_key = r2_private_runtime_key(config, "cloud-index.json.gz")
    data = {
        "version": 3,
        "provider": "r2",
        "runtimeKey": object_key,
        "updatedAt": datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z"),
        "limitBytes": r2_cloud_limit_bytes(config),
        "docs": index.get("docs") if isinstance(index.get("docs"), list) else [],
        "deletedDocIds": sorted(clean_doc_ids(index.get("deletedDocIds") or [])),
        "pendingCleanup": normalize_r2_cleanup_entries(index.get("pendingCleanup")),
    }
    raw = json.dumps(data, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    compressed = gzip.compress(raw, compresslevel=6, mtime=0)
    r2_put_object(config, object_key, compressed, "application/gzip")
    atomic_write_text(R2_AUTHORITATIVE_INDEX_FILE, raw.decode("utf-8"))
    set_path_hidden(R2_AUTHORITATIVE_INDEX_FILE)
    return {"objectKey": object_key, "bytes": len(compressed), "rawBytes": len(raw)}


def maybe_refresh_r2_authoritative_index(min_interval=30):
    global R2_AUTHORITY_REFRESHED_AT
    now = time.time()
    if now - R2_AUTHORITY_REFRESHED_AT < max(5, int(min_interval or 30)):
        return False
    # A status poll must never overwrite the local authority snapshot while a
    # publisher is committing a newer tombstone/index under the same lock.
    if not CLOUD_SYNC_SERIAL_LOCK.acquire(blocking=False):
        return False
    if not R2_AUTHORITY_REFRESH_LOCK.acquire(blocking=False):
        CLOUD_SYNC_SERIAL_LOCK.release()
        return False
    try:
        now = time.time()
        if now - R2_AUTHORITY_REFRESHED_AT < max(5, int(min_interval or 30)):
            return False
        config = load_r2_config()
        if r2_missing_fields(config):
            R2_AUTHORITY_REFRESHED_AT = now
            return False
        try:
            refresh_r2_authoritative_index(config)
            return True
        except Exception:
            return False
        finally:
            R2_AUTHORITY_REFRESHED_AT = now
    finally:
        R2_AUTHORITY_REFRESH_LOCK.release()
        CLOUD_SYNC_SERIAL_LOCK.release()


def r2_delete_objects(config, object_keys, batch_size=250):
    assert_r2_cloud_lease_active()
    keys = []
    seen = set()
    for object_key in object_keys or []:
        object_key = str(object_key or "").strip("/")
        if object_key and object_key not in seen:
            seen.add(object_key)
            keys.append(object_key)
    if not keys:
        return {"deleted_objects": 0, "errors": []}

    account_id = str(config.get("account_id") or "").strip()
    access_key = str(config.get("access_key_id") or "").strip()
    secret_key = str(config.get("secret_access_key") or "").strip()
    bucket = str(config.get("bucket") or "").strip()
    if not (account_id and access_key and secret_key and bucket):
        errors = []
        deleted = 0
        for object_key in keys:
            try:
                if r2_delete_object(config, object_key):
                    deleted += 1
                else:
                    errors.append({"object": object_key, "error": "R2 delete returned false"})
            except Exception as exc:
                errors.append({"object": object_key, "error": str(exc)})
        return {"deleted_objects": deleted, "errors": errors}

    host = f"{account_id}.r2.cloudflarestorage.com"
    canonical_uri = f"/{quote(bucket, safe='')}"
    url = f"https://{host}{canonical_uri}?delete"
    deleted = 0
    errors = []
    batch_size = max(1, min(1000, int(batch_size or 250)))

    def delete_batch(batch):
        body = (
            '<?xml version="1.0" encoding="UTF-8"?>'
            '<Delete xmlns="http://s3.amazonaws.com/doc/2006-03-01/">'
            + "".join(f"<Object><Key>{xml_escape(key)}</Key></Object>" for key in batch)
            + "<Quiet>true</Quiet></Delete>"
        ).encode("utf-8")
        payload_hash = hashlib.sha256(body).hexdigest()
        content_md5 = base64.b64encode(hashlib.md5(body).digest()).decode("ascii")
        now = datetime.now(timezone.utc)
        amz_date = now.strftime("%Y%m%dT%H%M%SZ")
        date_stamp = now.strftime("%Y%m%d")
        headers_lower = {
            "content-md5": content_md5,
            "content-type": "application/xml",
            "host": host,
            "x-amz-content-sha256": payload_hash,
            "x-amz-date": amz_date,
        }
        signed_headers = ";".join(sorted(headers_lower))
        canonical_headers = "".join(f"{key}:{headers_lower[key]}\n" for key in sorted(headers_lower))
        canonical_request = "\n".join([
            "POST",
            canonical_uri,
            "delete=",
            canonical_headers,
            signed_headers,
            payload_hash,
        ])
        scope = f"{date_stamp}/auto/s3/aws4_request"
        string_to_sign = "\n".join([
            "AWS4-HMAC-SHA256",
            amz_date,
            scope,
            hashlib.sha256(canonical_request.encode("utf-8")).hexdigest(),
        ])
        signature = hmac.new(r2_signing_key(secret_key, date_stamp), string_to_sign.encode("utf-8"), hashlib.sha256).hexdigest()
        headers = {
            "Content-MD5": content_md5,
            "Content-Type": "application/xml",
            "Host": host,
            "x-amz-content-sha256": payload_hash,
            "x-amz-date": amz_date,
            "Authorization": f"AWS4-HMAC-SHA256 Credential={access_key}/{scope}, SignedHeaders={signed_headers}, Signature={signature}",
        }
        request = urllib.request.Request(url, data=body, headers=headers, method="POST")
        with urllib.request.urlopen(request, timeout=180) as response:
            raw = response.read()
        batch_errors = []
        if raw:
            try:
                root = ET.fromstring(raw)
                for node in root.iter():
                    if str(node.tag).rsplit("}", 1)[-1] != "Error":
                        continue
                    fields = {
                        str(child.tag).rsplit("}", 1)[-1]: str(child.text or "")
                        for child in list(node)
                    }
                    batch_errors.append({
                        "object": fields.get("Key") or "",
                        "error": f"{fields.get('Code') or 'DeleteError'}: {fields.get('Message') or ''}".strip(),
                    })
            except ET.ParseError as exc:
                raise RuntimeError(f"Invalid R2 DeleteObjects response: {exc}") from exc
        return batch_errors

    for offset in range(0, len(keys), batch_size):
        batch = keys[offset:offset + batch_size]
        for attempt in range(1, 7):
            try:
                batch_errors = delete_batch(batch)
                if batch_errors:
                    errors.extend({**item, "offset": offset} for item in batch_errors)
                    deleted += max(0, len(batch) - len(batch_errors))
                else:
                    deleted += len(batch)
                if offset + batch_size < len(keys):
                    time.sleep(2.0)
                break
            except urllib.error.HTTPError as exc:
                if exc.code == 429 and attempt < 6:
                    time.sleep(min(75, 10 * attempt))
                    continue
                errors.append({"objects": len(batch), "offset": offset, "error": f"HTTP {exc.code}: {exc.reason}"})
                break
            except Exception as exc:
                errors.append({"objects": len(batch), "offset": offset, "error": str(exc)})
                break
    return {"deleted_objects": deleted, "errors": errors}


class R2CloudLeaseBusy(RuntimeError):
    pass


def _r2_lease_payload(token, purpose, ttl_seconds=240):
    now = datetime.now(timezone.utc)
    expires = datetime.fromtimestamp(now.timestamp() + ttl_seconds, timezone.utc)
    return {
        "version": 1,
        "token": token,
        "deviceId": current_device_id(),
        "deviceName": os.environ.get("COMPUTERNAME") or "device",
        "purpose": str(purpose or "cloud-sync"),
        "updatedAt": now.isoformat(timespec="milliseconds").replace("+00:00", "Z"),
        "expiresAt": expires.isoformat(timespec="milliseconds").replace("+00:00", "Z"),
    }


def acquire_r2_cloud_lease(config, purpose="cloud-sync", ttl_seconds=240):
    if not (str(config.get("access_key_id") or "").strip() and str(config.get("secret_access_key") or "").strip()):
        raise RuntimeError("Safe multi-device R2 sync requires S3 access keys")
    object_key = r2_private_runtime_key(config, "cloud-sync.lock")
    token = secrets.token_urlsafe(24)
    payload = _r2_lease_payload(token, purpose, ttl_seconds)
    body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    try:
        result = r2_put_object(
            config, object_key, body, "application/json; charset=utf-8",
            conditional_headers={"If-None-Match": "*"}, return_headers=True,
        )
        lease = {"config": config, "objectKey": object_key, "token": token, "etag": result.get("etag") or "", "purpose": purpose, "ttl": ttl_seconds}
        if not lease["etag"]:
            current_raw, lease["etag"] = r2_get_object_with_etag(config, object_key)
            current = json.loads(current_raw.decode("utf-8-sig"))
            if not secrets.compare_digest(str(current.get("token") or ""), token):
                return None
        return lease
    except urllib.error.HTTPError as exc:
        if exc.code not in (409, 412):
            raise

    try:
        existing_raw, existing_etag = r2_get_object_with_etag(config, object_key)
        existing = json.loads(existing_raw.decode("utf-8-sig"))
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            return acquire_r2_cloud_lease(config, purpose, ttl_seconds)
        raise
    except Exception:
        existing = {}
        existing_etag = ""
    if iso_timestamp(existing.get("expiresAt")) > time.time():
        return None
    if not existing_etag:
        return None
    try:
        result = r2_put_object(
            config, object_key, body, "application/json; charset=utf-8",
            conditional_headers={"If-Match": existing_etag}, return_headers=True,
        )
    except urllib.error.HTTPError as exc:
        if exc.code in (409, 412):
            return None
        raise
    lease = {"config": config, "objectKey": object_key, "token": token, "etag": result.get("etag") or "", "purpose": purpose, "ttl": ttl_seconds}
    if not lease["etag"]:
        current_raw, lease["etag"] = r2_get_object_with_etag(config, object_key)
        current = json.loads(current_raw.decode("utf-8-sig"))
        if not secrets.compare_digest(str(current.get("token") or ""), token):
            return None
    return lease


def renew_r2_cloud_lease(lease):
    payload = _r2_lease_payload(lease["token"], lease.get("purpose"), lease.get("ttl") or 240)
    body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    result = r2_put_object(
        lease["config"], lease["objectKey"], body, "application/json; charset=utf-8",
        conditional_headers={"If-Match": lease.get("etag") or ""}, return_headers=True,
    )
    lease["etag"] = result.get("etag") or lease.get("etag") or ""
    return lease


def release_r2_cloud_lease(lease):
    if not lease:
        return
    try:
        payload = _r2_lease_payload(lease["token"], "released", -1)
        body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        r2_put_object(
            lease["config"], lease["objectKey"], body, "application/json; charset=utf-8",
            conditional_headers={"If-Match": lease.get("etag") or ""}, return_headers=True,
        )
    except Exception:
        pass


def assert_r2_cloud_lease_active():
    with ACTIVE_R2_LEASE_LOCK:
        lease = ACTIVE_R2_LEASE
        if lease and lease.get("heartbeatError"):
            raise RuntimeError(f"R2 cloud lease was lost: {lease['heartbeatError']}")
        return True


@contextmanager
def r2_cloud_publish_lease(config, purpose="cloud-sync"):
    global ACTIVE_R2_LEASE
    lease = acquire_r2_cloud_lease(config, purpose)
    if not lease:
        raise R2CloudLeaseBusy("Máy còn lại đang cập nhật cloud; tác vụ này sẽ tự thử lại sau")
    stop_event = threading.Event()
    lease_lock = threading.Lock()

    def heartbeat():
        while not stop_event.wait(60):
            last_error = None
            for attempt in range(3):
                try:
                    with lease_lock:
                        renew_r2_cloud_lease(lease)
                    last_error = None
                    break
                except Exception as exc:
                    last_error = exc
                    if stop_event.wait(5 * (attempt + 1)):
                        return
            if last_error is not None:
                lease["heartbeatError"] = str(last_error)
                return

    thread = threading.Thread(target=heartbeat, name="r2-cloud-lease", daemon=True)
    thread.start()
    with ACTIVE_R2_LEASE_LOCK:
        ACTIVE_R2_LEASE = lease
    try:
        refresh_r2_authoritative_index(config)
        yield lease
        if lease.get("heartbeatError"):
            raise RuntimeError(f"R2 cloud lease was lost: {lease['heartbeatError']}")
    finally:
        with ACTIVE_R2_LEASE_LOCK:
            if ACTIVE_R2_LEASE is lease:
                ACTIVE_R2_LEASE = None
        stop_event.set()
        thread.join(timeout=2)
        with lease_lock:
            release_r2_cloud_lease(lease)


def r2_index_history_paths():
    sources = []
    if R2_CLOUD_INDEX_FILE.exists():
        sources.append(R2_CLOUD_INDEX_FILE)
    try:
        sources.extend(sorted(R2_DEVICE_INDEX_DIR.glob("*.json")))
    except Exception:
        pass
    return sources


def r2_index_source_paths():
    if R2_AUTHORITATIVE_INDEX_FILE.exists():
        return [R2_AUTHORITATIVE_INDEX_FILE]
    return r2_index_history_paths()


def r2_removal_source_paths():
    try:
        return sorted(R2_DEVICE_REMOVAL_DIR.glob("*.json"))
    except Exception:
        return []


def r2_record_fingerprint(record):
    if not isinstance(record, dict):
        return ""
    doc = record.get("doc") if isinstance(record.get("doc"), dict) else {}
    payload = {
        "docId": str(record.get("docId") or doc.get("id") or ""),
        "syncedAt": str(record.get("syncedAt") or ""),
        "bytes": int(record.get("bytes") or 0),
        "audioCount": int(record.get("audioCount") or 0),
        "objectPaths": sorted(str(path or "") for path in (record.get("objectPaths") or [])),
        "orphanPaths": sorted(str(path or "") for path in (record.get("orphanPaths") or [])),
        "model": str(doc.get("audioModelVersion") or ""),
        "voice": str(doc.get("audioVoice") or ""),
    }
    return hashlib.sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()


def normalize_r2_cleanup_entries(entries):
    """Normalize durable object-cleanup work stored in the authoritative index."""
    merged = {}
    for raw in entries if isinstance(entries, list) else []:
        if not isinstance(raw, dict):
            continue
        kind = str(raw.get("kind") or "").strip()
        doc_ids = sorted(clean_doc_ids(raw.get("docIds") or []))
        object_paths = sorted({
            str(path or "").strip("/")
            for path in (raw.get("objectPaths") or [])
            if str(path or "").strip("/")
        })
        raw_fingerprints = raw.get("fingerprintsByDoc") if isinstance(raw.get("fingerprintsByDoc"), dict) else {}
        fingerprints_by_doc = {
            str(doc_id or "").strip(): sorted({
                str(value or "").strip()
                for value in (values if isinstance(values, list) else [])
                if str(value or "").strip()
            })
            for doc_id, values in raw_fingerprints.items()
            if str(doc_id or "").strip()
        }
        fingerprints_by_doc = {key: value for key, value in fingerprints_by_doc.items() if value}
        if not kind or not doc_ids or (not object_paths and not fingerprints_by_doc):
            continue
        identity = {
            "kind": kind,
            "docIds": doc_ids,
            "fingerprintsByDoc": fingerprints_by_doc,
        }
        cleanup_id = str(raw.get("id") or "").strip()
        if not cleanup_id:
            cleanup_id = hashlib.sha256(
                json.dumps(identity, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
            ).hexdigest()[:24]
        current = merged.get(cleanup_id)
        if current is None:
            merged[cleanup_id] = {
                "id": cleanup_id,
                "kind": kind,
                "docIds": doc_ids,
                "objectPaths": object_paths,
                "fingerprintsByDoc": fingerprints_by_doc,
                "createdAt": str(raw.get("createdAt") or datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")),
            }
            continue
        current["docIds"] = sorted(set(current.get("docIds") or []) | set(doc_ids))
        current["objectPaths"] = sorted(set(current.get("objectPaths") or []) | set(object_paths))
        grouped = current.get("fingerprintsByDoc") if isinstance(current.get("fingerprintsByDoc"), dict) else {}
        for doc_id, values in fingerprints_by_doc.items():
            grouped[doc_id] = sorted(set(grouped.get(doc_id) or []) | set(values))
        current["fingerprintsByDoc"] = grouped
    return sorted(merged.values(), key=lambda item: (item.get("createdAt") or "", item.get("id") or ""))


def r2_cleanup_entry(kind, doc_ids, object_paths, records=None):
    grouped = {}
    for record in records or []:
        if not isinstance(record, dict):
            continue
        doc = record.get("doc") if isinstance(record.get("doc"), dict) else {}
        doc_id = str(record.get("docId") or doc.get("id") or "").strip()
        fingerprint = r2_record_fingerprint(record)
        if doc_id and fingerprint:
            grouped.setdefault(doc_id, set()).add(fingerprint)
    payload = {
        "kind": str(kind or "").strip(),
        "docIds": sorted(clean_doc_ids(doc_ids)),
        "objectPaths": sorted({str(path or "").strip("/") for path in (object_paths or []) if str(path or "").strip("/")}),
        "fingerprintsByDoc": {doc_id: sorted(values) for doc_id, values in sorted(grouped.items())},
        "createdAt": datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z"),
    }
    return normalize_r2_cleanup_entries([payload])[0] if payload["docIds"] and (payload["objectPaths"] or payload["fingerprintsByDoc"]) else None


def upsert_r2_cleanup_entry(index, entry):
    if not isinstance(index, dict) or not isinstance(entry, dict):
        return None
    entries = normalize_r2_cleanup_entries([*(index.get("pendingCleanup") or []), entry])
    index["pendingCleanup"] = entries
    cleanup_id = str(entry.get("id") or "")
    return next((item for item in entries if item.get("id") == cleanup_id), None)


def r2_cleanup_entries_for(index, kind, doc_ids):
    ids = set(clean_doc_ids(doc_ids))
    return [
        item for item in normalize_r2_cleanup_entries(index.get("pendingCleanup") if isinstance(index, dict) else [])
        if item.get("kind") == kind and ids.intersection(item.get("docIds") or [])
    ]


def update_r2_cleanup_entries(index, cleanup_ids, failed_paths=None, remove=False):
    ids = {str(value or "").strip() for value in (cleanup_ids or []) if str(value or "").strip()}
    failed = {str(path or "").strip("/") for path in (failed_paths or []) if str(path or "").strip("/")}
    entries = []
    for item in normalize_r2_cleanup_entries(index.get("pendingCleanup") if isinstance(index, dict) else []):
        if item.get("id") not in ids:
            entries.append(item)
            continue
        if remove:
            continue
        item = dict(item)
        item["objectPaths"] = sorted(set(item.get("objectPaths") or []) & failed)
        if item["objectPaths"]:
            entries.append(item)
    index["pendingCleanup"] = entries
    return entries


def r2_failed_delete_paths(requested_paths, delete_result):
    requested = {str(path or "").strip("/") for path in (requested_paths or []) if str(path or "").strip("/")}
    errors = delete_result.get("errors") if isinstance(delete_result, dict) and isinstance(delete_result.get("errors"), list) else []
    named = {
        str(item.get("object") or "").strip("/")
        for item in errors
        if isinstance(item, dict) and str(item.get("object") or "").strip("/")
    }
    generic = any(not isinstance(item, dict) or not str(item.get("object") or "").strip("/") for item in errors)
    try:
        deleted_objects = int(delete_result.get("deleted_objects") or 0)
    except Exception:
        deleted_objects = 0
    if generic or deleted_objects < max(0, len(requested) - len(named)):
        return requested
    return requested & named


def read_r2_removal_markers(sources=None):
    markers = set()
    for source in (list(sources) if sources is not None else r2_removal_source_paths()):
        try:
            data = json.loads(source.read_text(encoding="utf-8-sig"))
        except Exception:
            continue
        records = data.get("records") if isinstance(data, dict) else {}
        if not isinstance(records, dict):
            continue
        for fingerprints in records.values():
            if isinstance(fingerprints, list):
                markers.update(str(value or "").strip() for value in fingerprints if str(value or "").strip())
    return markers


def remember_r2_removed_records(removed_records):
    grouped = {}
    for record in removed_records or []:
        if not isinstance(record, dict):
            continue
        doc = record.get("doc") if isinstance(record.get("doc"), dict) else {}
        doc_id = str(record.get("docId") or doc.get("id") or "").strip()
        fingerprint = r2_record_fingerprint(record)
        if doc_id and fingerprint:
            grouped.setdefault(doc_id, set()).add(fingerprint)
    return remember_r2_removed_fingerprints(grouped)


def remember_r2_removed_fingerprints(grouped):
    global R2_INDEX_CACHE_SIGNATURE, R2_INDEX_CACHE_DATA, R2_INDEX_CACHE_TOTAL_BYTES
    normalized = {
        str(doc_id or "").strip(): {
            str(value or "").strip()
            for value in (values if isinstance(values, (list, set, tuple)) else [])
            if str(value or "").strip()
        }
        for doc_id, values in (grouped.items() if isinstance(grouped, dict) else [])
        if str(doc_id or "").strip()
    }
    normalized = {doc_id: values for doc_id, values in normalized.items() if values}
    if not normalized:
        return {}
    now_iso = datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")
    device_id = current_device_id()
    target = R2_DEVICE_REMOVAL_DIR / f"{device_id}.json"
    with R2_INDEX_CACHE_LOCK:
        try:
            data = json.loads(target.read_text(encoding="utf-8-sig")) if target.exists() else {}
        except Exception:
            data = {}
        records = data.get("records") if isinstance(data.get("records"), dict) else {}
        for doc_id, fingerprints in normalized.items():
            records[doc_id] = sorted(set(records.get(doc_id) if isinstance(records.get(doc_id), list) else []) | fingerprints)
        payload = {
            "version": 2,
            "deviceId": device_id,
            "updatedAt": now_iso,
            "records": records,
        }
        R2_DEVICE_REMOVAL_DIR.mkdir(parents=True, exist_ok=True)
        atomic_write_text(target, json.dumps(payload, ensure_ascii=False, indent=2), retries=12)
        R2_INDEX_CACHE_SIGNATURE = None
        R2_INDEX_CACHE_DATA = None
        R2_INDEX_CACHE_TOTAL_BYTES = 0
    set_path_hidden(R2_DEVICE_REMOVAL_DIR)
    set_path_hidden(target)
    return records


def read_all_r2_records(doc_ids=None, include_removed=False):
    target_ids = set(clean_doc_ids(doc_ids)) if doc_ids is not None else None
    removed = set() if include_removed else read_r2_removal_markers()
    records = []
    seen = set()
    sources = list(dict.fromkeys([R2_AUTHORITATIVE_INDEX_FILE, *r2_index_history_paths()]))
    for source in sources:
        if not source.exists():
            continue
        try:
            data = json.loads(source.read_text(encoding="utf-8-sig"))
        except Exception:
            continue
        for record in data.get("docs") if isinstance(data, dict) and isinstance(data.get("docs"), list) else []:
            if not isinstance(record, dict):
                continue
            doc = record.get("doc") if isinstance(record.get("doc"), dict) else {}
            doc_id = str(record.get("docId") or doc.get("id") or "").strip()
            fingerprint = r2_record_fingerprint(record)
            if not doc_id or (target_ids is not None and doc_id not in target_ids):
                continue
            if fingerprint in removed or fingerprint in seen:
                continue
            seen.add(fingerprint)
            records.append(record)
    return records


def _load_r2_cloud_index_cached():
    global R2_INDEX_CACHE_SIGNATURE, R2_INDEX_CACHE_DATA, R2_INDEX_CACHE_TOTAL_BYTES
    sources = r2_index_source_paths()
    removal_sources = r2_removal_source_paths()
    signature = (file_set_signature(sources), file_set_signature(removal_sources))
    with R2_INDEX_CACHE_LOCK:
        if R2_INDEX_CACHE_DATA is not None and signature == R2_INDEX_CACHE_SIGNATURE:
            return R2_INDEX_CACHE_DATA
    merged_records = {}
    merged_scores = {}
    pending_cleanup = []
    remote_deleted_doc_ids = set()
    removal_markers = read_r2_removal_markers(removal_sources)
    limit_bytes = r2_cloud_limit_bytes()
    newest_updated_at = ""
    for source in sources:
        source_priority = 1 if source == R2_AUTHORITATIVE_INDEX_FILE else 0
        try:
            data = json.loads(source.read_text(encoding="utf-8-sig"))
        except Exception:
            continue
        if not isinstance(data, dict):
            continue
        newest_updated_at = max(newest_updated_at, str(data.get("updatedAt") or ""))
        try:
            limit_bytes = max(limit_bytes, int(data.get("limitBytes") or 0))
        except Exception:
            pass
        pending_cleanup.extend(normalize_r2_cleanup_entries(data.get("pendingCleanup")))
        remote_deleted_doc_ids.update(clean_doc_ids(data.get("deletedDocIds") or []))
        for record in data.get("docs") if isinstance(data.get("docs"), list) else []:
            if not isinstance(record, dict):
                continue
            if r2_record_fingerprint(record) in removal_markers:
                continue
            doc = record.get("doc") if isinstance(record.get("doc"), dict) else {}
            doc_id = str(record.get("docId") or doc.get("id") or "").strip()
            if not doc_id:
                continue
            existing = merged_records.get(doc_id)
            candidate_time = bounded_client_timestamp(record.get("syncedAt"))
            candidate_score = (candidate_time, len(record.get("objectPaths") or []), int(record.get("audioCount") or 0), source_priority)
            existing_score = merged_scores.get(doc_id, (-1, -1, -1, -1))
            if existing is None or candidate_score >= existing_score:
                merged_records[doc_id] = record
                merged_scores[doc_id] = candidate_score
    cleanup_entries = []
    for item in normalize_r2_cleanup_entries(pending_cleanup):
        fingerprints = {
            value
            for values in (item.get("fingerprintsByDoc") or {}).values()
            for value in values
        }
        if fingerprints and fingerprints.issubset(removal_markers):
            continue
        cleanup_entries.append(item)
    data = {
        "version": 3,
        "provider": "r2",
        "updatedAt": newest_updated_at,
        "limitBytes": limit_bytes,
        "docs": list(merged_records.values()),
        "deletedDocIds": sorted(remote_deleted_doc_ids),
        "pendingCleanup": cleanup_entries,
    }
    with R2_INDEX_CACHE_LOCK:
        R2_INDEX_CACHE_SIGNATURE = signature
        R2_INDEX_CACHE_DATA = data
        R2_INDEX_CACHE_TOTAL_BYTES = r2_cloud_index_total_bytes(data)
        return R2_INDEX_CACHE_DATA


def read_r2_cloud_index(include_deleted=False):
    data = copy.deepcopy(_load_r2_cloud_index_cached())
    if include_deleted:
        return data
    deleted_ids = set(read_cloud_deleted_doc_ids()) | set(clean_doc_ids(data.get("deletedDocIds") or []))
    if deleted_ids:
        data["docs"] = [
            item for item in (data.get("docs") or [])
            if str(item.get("docId") or (item.get("doc") or {}).get("id") or "").strip() not in deleted_ids
        ]
    return data


def r2_cached_total_bytes():
    data = _load_r2_cloud_index_cached()
    deleted_ids = set(read_cloud_deleted_doc_ids()) | set(clean_doc_ids(data.get("deletedDocIds") or []))
    if not deleted_ids:
        return r2_cloud_index_total_bytes(data)
    return sum(
        int(item.get("bytes") or 0)
        for item in (data.get("docs") or [])
        if str(item.get("docId") or (item.get("doc") or {}).get("id") or "").strip() not in deleted_ids
    )


def r2_remote_audio_url(filename):
    """Return the already-uploaded R2 URL when this machine lacks a local WAV."""
    global R2_REMOTE_AUDIO_SOURCE_ID, R2_REMOTE_AUDIO_MAP
    name = Path(str(filename or "")).name
    if not name:
        return ""
    data = _load_r2_cloud_index_cached()
    deleted_ids = set(read_cloud_deleted_doc_ids()) | set(clean_doc_ids(data.get("deletedDocIds") or []))
    source_id = (id(data), tuple(sorted(deleted_ids)), file_set_signature(deleted_doc_source_paths()))
    with R2_INDEX_CACHE_LOCK:
        if R2_REMOTE_AUDIO_SOURCE_ID != source_id:
            mapping = {}
            for record in data.get("docs") if isinstance(data.get("docs"), list) else []:
                doc = record.get("doc") if isinstance(record, dict) and isinstance(record.get("doc"), dict) else {}
                doc_id = str(record.get("docId") or doc.get("id") or "").strip() if isinstance(record, dict) else ""
                if doc_id and doc_id in deleted_ids:
                    continue
                manifest = doc.get("audioManifest") if isinstance(doc.get("audioManifest"), dict) else {}
                values = list(manifest.values())
                values.extend(
                    item.get("url")
                    for item in (doc.get("audioItems") if isinstance(doc.get("audioItems"), list) else [])
                    if isinstance(item, dict)
                )
                for value in values:
                    url = str(value or "").strip()
                    if not url.lower().startswith(("https://", "http://")):
                        continue
                    remote_name = Path(url.split("?", 1)[0]).name
                    if remote_name:
                        mapping.setdefault(remote_name, url)
            R2_REMOTE_AUDIO_MAP = mapping
            R2_REMOTE_AUDIO_SOURCE_ID = source_id
        return str(R2_REMOTE_AUDIO_MAP.get(name) or "")


def hydrate_remote_audio(filename, max_bytes=256 * 1024 * 1024):
    name = Path(str(filename or "")).name
    existing = resolve_audio_path(name)
    if existing:
        return existing
    remote_url = r2_remote_audio_url(name)
    if not remote_url:
        return None
    with REMOTE_AUDIO_DOWNLOAD_LOCK:
        lock = REMOTE_AUDIO_DOWNLOAD_LOCKS.setdefault(name, threading.Lock())
    with lock:
        existing = resolve_audio_path(name)
        if existing:
            return existing
        request = urllib.request.Request(remote_url, headers={"User-Agent": "LocalReader/3.1"})
        with urllib.request.urlopen(request, timeout=90) as response:
            length = int(response.headers.get("Content-Length") or 0)
            if length > max_bytes:
                raise ValueError("Remote audio is too large")
            data = response.read(max_bytes + 1)
        if not data or len(data) > max_bytes:
            raise ValueError("Remote audio is empty or too large")
        target = resolve_audio_path(name, require_existing=False)
        tmp = target.with_name(f"{target.stem}.{os.getpid()}.{threading.get_ident()}.{time.time_ns()}.tmp{target.suffix}")
        try:
            tmp.write_bytes(data)
            tmp.replace(target)
        finally:
            try:
                tmp.unlink(missing_ok=True)
            except Exception:
                pass
        return target


def write_r2_cloud_index(index):
    global R2_INDEX_CACHE_SIGNATURE, R2_INDEX_CACHE_DATA, R2_INDEX_CACHE_TOTAL_BYTES
    assert_r2_cloud_lease_active()
    device_id = current_device_id()
    R2_DEVICE_INDEX_DIR.mkdir(parents=True, exist_ok=True)
    target = R2_DEVICE_INDEX_DIR / f"{device_id}.json"
    data = {
        "version": 3,
        "provider": "r2",
        "deviceId": device_id,
        "deviceName": os.environ.get("COMPUTERNAME") or "device",
        "updatedAt": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "limitBytes": r2_cloud_limit_bytes(),
        "docs": index.get("docs") if isinstance(index.get("docs"), list) else [],
        "deletedDocIds": sorted(
            set(clean_doc_ids(index.get("deletedDocIds") or []))
            | set(read_cloud_deleted_doc_ids())
        ),
        "pendingCleanup": normalize_r2_cleanup_entries(index.get("pendingCleanup")),
    }
    payload = json.dumps(data, ensure_ascii=False, indent=2)
    atomic_write_text(target, payload, retries=12)
    with R2_INDEX_CACHE_LOCK:
        R2_INDEX_CACHE_SIGNATURE = None
        R2_INDEX_CACHE_DATA = None
        R2_INDEX_CACHE_TOTAL_BYTES = 0
    try:
        if os.name == "nt":
            subprocess.run(["attrib", "+h", str(R2_DEVICE_INDEX_DIR)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=3)
            subprocess.run(["attrib", "+h", str(target)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=3)
    except Exception:
        pass
    with ACTIVE_R2_LEASE_LOCK:
        lease_config = ACTIVE_R2_LEASE.get("config") if isinstance(ACTIVE_R2_LEASE, dict) else None
    if lease_config:
        publish_r2_authoritative_index(lease_config, data)
    return data


def r2_cloud_index_total_bytes(index):
    return sum(int(item.get("bytes") or 0) for item in (index.get("docs") or []))


def prune_r2_cloud_index(config, protected_doc_id=""):
    # Auto-pruning was only useful when cloud storage was capped very low.
    # Keep R2 files unless the user explicitly deletes a file/folder in the app.
    return []


def local_doc_meta_map():
    try:
        store = read_project_store()
        docs = store.get("docs") if isinstance(store.get("docs"), list) else []
    except Exception:
        docs = []
    meta = {}
    for idx, doc in enumerate(docs):
        if not isinstance(doc, dict):
            continue
        doc_id = str(doc.get("id") or "").strip()
        if not doc_id:
            continue
        try:
            desktop_order = int(doc.get("desktopOrder"))
        except Exception:
            desktop_order = idx
        meta[doc_id] = {
            "desktopOrder": desktop_order,
            "folderPath": doc.get("folderPath") or "",
            "title": doc.get("title") or "",
        }
    return meta


def ordered_cloud_records(index):
    meta = local_doc_meta_map()
    records = [item for item in (index.get("docs") or []) if isinstance(item, dict)]
    ordered = []
    for fallback_order, item in enumerate(records):
        doc = item.get("doc") if isinstance(item.get("doc"), dict) else {}
        doc_id = str(item.get("docId") or doc.get("id") or "").strip()
        if not doc_id:
            continue
        if cloud_doc_id_is_deleted(doc_id):
            continue
        local_meta = meta.get(doc_id) or {}
        try:
            desktop_order = int(local_meta.get("desktopOrder"))
        except Exception:
            try:
                desktop_order = int(doc.get("desktopOrder"))
            except Exception:
                try:
                    desktop_order = int(item.get("desktopOrder"))
                except Exception:
                    desktop_order = 1000000 + fallback_order
        if isinstance(doc, dict):
            doc["desktopOrder"] = desktop_order
            if local_meta.get("folderPath") is not None:
                doc["folderPath"] = local_meta.get("folderPath") or doc.get("folderPath") or ""
            if local_meta.get("title"):
                doc["title"] = local_meta.get("title")
        item["desktopOrder"] = desktop_order
        ordered.append((desktop_order, fallback_order, item))
    return [item for _, _, item in sorted(ordered, key=lambda row: (row[0], row[1]))]


def upload_r2_cloud_library(config, index=None):
    index = index or read_r2_cloud_index()
    docs = ordered_cloud_records(index)
    library_docs = []
    for item in docs:
        if int(item.get("audioCount") or 0) <= 0 or not isinstance(item.get("doc"), dict):
            continue
        doc = dict(item.get("doc") or {})
        audio_items = [
            {
                "text": str(audio_item.get("text") or ""),
                "url": str(audio_item.get("url") or ""),
            }
            for audio_item in (doc.get("audioItems") if isinstance(doc.get("audioItems"), list) else [])
            if isinstance(audio_item, dict) and audio_item.get("text") and audio_item.get("url")
        ]
        if not audio_items:
            continue
        doc["audioItems"] = audio_items
        # `audioItems` already contains the exact text and URL for every playable
        # chunk. Do not duplicate the same multi-megabyte text and manifest in the
        # global mobile library; old per-project manifests remain available.
        doc.pop("text", None)
        doc.pop("audioManifest", None)
        library_docs.append(doc)
    payload = {"version": 2, "updatedAt": time.strftime("%Y-%m-%dT%H:%M:%S"), "app": "Local Reader Cloud Library", "provider": "r2", "limitBytes": r2_cloud_limit_bytes(config), "usedBytes": r2_cloud_index_total_bytes(index), "docs": library_docs}
    data = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    r2_put_object(config, r2_object_key(config, "library.json"), data, "application/json; charset=utf-8")
    return r2_public_url(config, "library.json")


def update_r2_latest_pointer(config, index=None):
    index = index or read_r2_cloud_index()
    records = ordered_cloud_records(index)
    if not records:
        result = r2_delete_objects(config, [r2_object_key(config, "latest.json")])
        return {"ok": not result.get("errors"), "errors": result.get("errors") or [], "url": ""}
    record = records[-1]
    doc = record.get("doc") if isinstance(record.get("doc"), dict) else {}
    payload = {
        "version": 1,
        "syncedAt": record.get("syncedAt") or time.strftime("%Y-%m-%dT%H:%M:%S"),
        "app": "Local Reader Cloud",
        "provider": "r2",
        "doc": doc,
        "missingAudio": [],
    }
    data = json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")
    r2_put_object(config, r2_object_key(config, "latest.json"), data, "application/json; charset=utf-8")
    return {"ok": True, "errors": [], "url": r2_public_url(config, "latest.json")}


def r2_player_asset_files():
    assets = []
    for player_name in ("cloud_player.html", "mobile_player.html"):
        player = ROOT / player_name
        if player.is_file():
            assets.append((player_name, player))
    cloud_mobile = ROOT / "CloudMobilePlayer" / "index.html"
    if cloud_mobile.is_file():
        assets.append(("CloudMobilePlayer/index.html", cloud_mobile))
    katex_root = ROOT / "vendor" / "katex"
    if katex_root.exists():
        for asset_path in katex_root.rglob("*"):
            if not asset_path.is_file():
                continue
            rel = asset_path.relative_to(ROOT).as_posix()
            assets.append((rel, asset_path))
    return assets


def upload_r2_player_assets(config):
    for rel, asset_path in r2_player_asset_files():
        ctype = "text/html; charset=utf-8" if asset_path.suffix.lower() == ".html" else (mimetypes.guess_type(str(asset_path))[0] or "application/octet-stream")
        if asset_path.suffix.lower() == ".woff2":
            ctype = "font/woff2"
        r2_put_object(config, r2_object_key(config, rel), asset_path.read_bytes(), ctype)


def r2_player_assets_signature(config):
    digest = hashlib.sha256()
    scope = {
        "account": str(config.get("account_id") or ""),
        "bucket": str(config.get("bucket") or ""),
        "prefix": str(config.get("prefix") or ""),
        "public": str(config.get("public_base_url") or ""),
        "build": APP_BUILD_ID,
    }
    digest.update(json.dumps(scope, sort_keys=True).encode("utf-8"))
    for rel, asset_path in r2_player_asset_files():
        digest.update(rel.encode("utf-8"))
        digest.update(hashlib.sha256(asset_path.read_bytes()).digest())
    return digest.hexdigest()


def ensure_r2_player_assets(config, force=False):
    with R2_ASSET_UPLOAD_LOCK:
        signature = r2_player_assets_signature(config)
        try:
            state = json.loads(R2_PLAYER_ASSET_STATE_FILE.read_text(encoding="utf-8-sig"))
        except Exception:
            state = {}
        if not force and state.get("signature") == signature:
            return {"uploaded": False, "signature": signature}
        upload_r2_player_assets(config)
        payload = {
            "version": 1,
            "signature": signature,
            "build": APP_BUILD_ID,
            "savedAt": datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z"),
        }
        atomic_write_text(R2_PLAYER_ASSET_STATE_FILE, json.dumps(payload, ensure_ascii=False, indent=2))
        return {"uploaded": True, "signature": signature}


def update_r2_cloud_index_after_sync(config, record, rebuild_library=True):
    index = read_r2_cloud_index()
    docs = [item for item in (index.get("docs") or []) if item.get("docId") != record.get("docId")]
    docs.append(record)
    index["docs"] = docs
    write_r2_cloud_index(index)
    deleted = []
    library_url = r2_public_url(config, "library.json")
    if rebuild_library:
        index = read_r2_cloud_index()
        library_url = upload_r2_cloud_library(config, index)
    return index, deleted, library_url


def clean_doc_ids(doc_ids):
    if isinstance(doc_ids, (str, int)):
        doc_ids = [doc_ids]
    cleaned = []
    seen = set()
    for item in (doc_ids or []):
        doc_id = str(item or "").strip()
        if doc_id and doc_id not in seen:
            seen.add(doc_id)
            cleaned.append(doc_id)
    return cleaned


def deleted_doc_source_paths():
    sources = []
    if CLOUD_DELETED_DOC_IDS_FILE.exists():
        sources.append(CLOUD_DELETED_DOC_IDS_FILE)
    try:
        sources.extend(sorted(DEVICE_DELETION_DIR.glob("*.json")))
    except Exception:
        pass
    return sources


def read_cloud_deleted_doc_ids():
    global DELETION_CACHE_SIGNATURE, DELETION_CACHE_IDS
    sources = deleted_doc_source_paths()
    signature = file_set_signature(sources)
    with DELETION_LOCK:
        if signature != DELETION_CACHE_SIGNATURE:
            deleted = set()
            for source in sources:
                try:
                    data = json.loads(source.read_text(encoding="utf-8-sig"))
                    ids = data.get("docIds") if isinstance(data, dict) else data
                    deleted.update(clean_doc_ids(ids if isinstance(ids, list) else []))
                except Exception:
                    continue
            DELETION_CACHE_IDS = frozenset(deleted)
            DELETION_CACHE_SIGNATURE = signature
        return set(DELETION_CACHE_IDS)


def write_cloud_deleted_doc_ids(ids):
    global DELETION_CACHE_SIGNATURE, DELETION_CACHE_IDS, PROJECT_CACHE_SIGNATURE, PROJECT_CACHE_STORE
    cleaned = sorted(clean_doc_ids(list(ids or [])))
    device_id = current_device_id()
    DEVICE_DELETION_DIR.mkdir(parents=True, exist_ok=True)
    target = DEVICE_DELETION_DIR / f"{device_id}.json"
    payload = json.dumps({"version": 2, "deviceId": device_id, "updatedAt": datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z"), "docIds": cleaned}, ensure_ascii=False, indent=2)
    with DELETION_LOCK:
        atomic_write_text(target, payload, retries=12)
        DELETION_CACHE_SIGNATURE = None
        DELETION_CACHE_IDS = frozenset()
        PROJECT_CACHE_SIGNATURE = None
        PROJECT_CACHE_STORE = None
    try:
        if os.name == "nt":
            subprocess.run(["attrib", "+h", str(DEVICE_DELETION_DIR)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=3)
            subprocess.run(["attrib", "+h", str(target)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=3)
    except Exception:
        pass
    return set(cleaned)


def remember_cloud_deleted_doc_ids(doc_ids):
    ids = clean_doc_ids(doc_ids)
    if not ids:
        return read_cloud_deleted_doc_ids()
    with DELETION_LOCK:
        deleted = read_cloud_deleted_doc_ids()
        deleted.update(ids)
        return write_cloud_deleted_doc_ids(deleted)


def cloud_doc_id_is_deleted(doc_id):
    doc_id = str(doc_id or "").strip()
    if not doc_id:
        return False
    if doc_id in read_cloud_deleted_doc_ids():
        return True
    try:
        remote_ids = clean_doc_ids(read_r2_cloud_index(include_deleted=True).get("deletedDocIds") or [])
        return doc_id in remote_ids
    except Exception:
        return False


def delete_projects_from_r2(doc_ids):
    config = load_r2_config()
    missing = r2_missing_fields(config)
    if missing:
        return {"ok": False, "configured": False, "provider": "r2", "missing": missing, "error": "Missing R2 config fields"}
    ids = clean_doc_ids(doc_ids)
    index = read_r2_cloud_index(include_deleted=True)
    docs = index.get("docs") if isinstance(index.get("docs"), list) else []
    if not ids:
        library_url = upload_r2_cloud_library(config, index)
        return {"ok": True, "configured": True, "provider": "r2", "deleted": [], "deleted_objects": 0, "delete_errors": [], "library_url": library_url, "used_bytes": r2_cloud_index_total_bytes(index), "limit_bytes": r2_cloud_limit_bytes(config)}
    existing_cleanup = r2_cleanup_entries_for(index, "project-delete", ids)
    ids = sorted(set(ids) | {
        doc_id
        for entry in existing_cleanup
        for doc_id in (entry.get("docIds") or [])
    })
    remember_cloud_deleted_doc_ids(ids)
    index["deletedDocIds"] = sorted(set(clean_doc_ids(index.get("deletedDocIds") or [])) | set(ids))
    target_ids = set(ids)
    remaining = []
    deleted = []
    target_records = []
    object_keys_to_delete = []
    for item in docs:
        item_doc = item.get("doc") if isinstance(item.get("doc"), dict) else {}
        doc_id = str(item.get("docId") or item_doc.get("id") or "").strip()
        if doc_id not in target_ids:
            remaining.append(item)
            continue
        object_paths = []
        seen_paths = set()
        for object_key in list(item.get("objectPaths") or []) + list(item.get("orphanPaths") or []):
            object_key = str(object_key or "").strip("/")
            if object_key and object_key not in seen_paths:
                seen_paths.add(object_key)
                object_paths.append(object_key)
        object_keys_to_delete.extend(object_paths)
        target_records.append(item)
        deleted.append({"docId": doc_id, "title": item.get("title") or item_doc.get("title") or "", "objects": len(object_paths), "bytes": int(item.get("bytes") or 0)})
    # A losing per-device index can hold a different audio revision. Union all
    # paths so a user deletion removes every public object, not only the winner.
    for record in read_all_r2_records(ids, include_removed=True):
        fingerprint = r2_record_fingerprint(record)
        if fingerprint and all(r2_record_fingerprint(item) != fingerprint for item in target_records):
            target_records.append(record)
        object_keys_to_delete.extend(list(record.get("objectPaths") or []) + list(record.get("orphanPaths") or []))
    object_keys_to_delete = {
        str(path or "").strip("/")
        for path in object_keys_to_delete
        if str(path or "").strip("/")
    }
    protected_paths = {
        str(path or "").strip("/")
        for item in remaining
        for path in (item.get("objectPaths") or [])
        if str(path or "").strip("/")
    }
    object_keys_to_delete.difference_update(protected_paths)
    cleanup_entry = r2_cleanup_entry("project-delete", ids, object_keys_to_delete, target_records)
    if cleanup_entry:
        upsert_r2_cleanup_entry(index, cleanup_entry)
    cleanup_entries = r2_cleanup_entries_for(index, "project-delete", ids)
    cleanup_ids = [entry.get("id") for entry in cleanup_entries]
    object_keys_to_delete.update(
        str(path or "").strip("/")
        for entry in cleanup_entries
        for path in (entry.get("objectPaths") or [])
        if str(path or "").strip("/")
    )
    object_keys_to_delete.difference_update(protected_paths)

    # Commit authority and both public pointers before removing any object that
    # an older mobile library could still reference.
    index["docs"] = remaining
    if cleanup_ids:
        update_r2_cleanup_entries(index, cleanup_ids, object_keys_to_delete)
    write_r2_cloud_index(index)
    filtered_index = read_r2_cloud_index()
    library_url = upload_r2_cloud_library(config, filtered_index)
    latest_result = update_r2_latest_pointer(config, filtered_index)
    delete_errors = list(latest_result.get("errors") or [])
    if delete_errors:
        return {"ok": False, "configured": True, "provider": "r2", "requested": ids, "deleted": [], "pending_retry": deleted or ids, "deleted_objects": 0, "delete_errors": delete_errors, "used_bytes": r2_cached_total_bytes(), "limit_bytes": r2_cloud_limit_bytes(config), "library_url": library_url, "latest_url": latest_result.get("url") or "", "player_url": r2_public_url(config, "cloud_player.html"), "mobile_player_url": r2_public_url(config, "CloudMobilePlayer/index.html")}

    requested_paths = sorted(object_keys_to_delete)
    delete_result = r2_delete_objects(config, requested_paths)
    failed_paths = r2_failed_delete_paths(requested_paths, delete_result)
    delete_errors = list(delete_result.get("errors") or [])
    delete_ok = not failed_paths
    final_index = read_r2_cloud_index(include_deleted=True)
    if delete_ok:
        grouped = {}
        for entry in cleanup_entries:
            for doc_id, fingerprints in (entry.get("fingerprintsByDoc") or {}).items():
                grouped.setdefault(doc_id, set()).update(fingerprints)
        for record in target_records:
            record_doc = record.get("doc") if isinstance(record.get("doc"), dict) else {}
            record_id = str(record.get("docId") or record_doc.get("id") or "").strip()
            fingerprint = r2_record_fingerprint(record)
            if record_id and fingerprint:
                grouped.setdefault(record_id, set()).add(fingerprint)
        remember_r2_removed_fingerprints(grouped)
        final_index = read_r2_cloud_index(include_deleted=True)
        update_r2_cleanup_entries(final_index, cleanup_ids, remove=True)
    else:
        update_r2_cleanup_entries(final_index, cleanup_ids, failed_paths)
    write_r2_cloud_index(final_index)
    return {"ok": delete_ok, "configured": True, "provider": "r2", "requested": ids, "deleted": deleted if delete_ok else [], "pending_retry": [] if delete_ok else (deleted or ids), "deleted_objects": delete_result.get("deleted_objects") or 0, "delete_errors": delete_errors, "used_bytes": r2_cached_total_bytes(), "limit_bytes": r2_cloud_limit_bytes(config), "library_url": library_url, "latest_url": latest_result.get("url") or "", "player_url": r2_public_url(config, "cloud_player.html"), "mobile_player_url": r2_public_url(config, "CloudMobilePlayer/index.html")}


def delete_reset_cloud_records_from_r2(doc_ids=None):
    config = load_r2_config()
    missing = r2_missing_fields(config)
    if missing:
        return {"ok": False, "configured": False, "provider": "r2", "missing": missing, "error": "Missing R2 config fields"}
    if doc_ids is None:
        store = read_project_store()
        docs = store.get("docs") if isinstance(store.get("docs"), list) else []
        ids = clean_doc_ids([doc.get("id") for doc in docs if isinstance(doc, dict) and doc.get("audioResetAt")])
    else:
        ids = clean_doc_ids(doc_ids)
    index = read_r2_cloud_index(include_deleted=True)
    existing_cleanup = r2_cleanup_entries_for(index, "audio-reset", ids)
    ids = sorted(set(ids) | {
        doc_id
        for entry in existing_cleanup
        for doc_id in (entry.get("docIds") or [])
    })
    if not ids:
        return {"ok": True, "configured": True, "provider": "r2", "deleted": [], "deleted_objects": 0, "used_bytes": r2_cached_total_bytes(), "limit_bytes": r2_cloud_limit_bytes(config)}

    docs = index.get("docs") if isinstance(index.get("docs"), list) else []
    all_records = read_all_r2_records(ids)
    stale_records = []
    current_records = []
    for item in all_records:
        item_doc = item.get("doc") if isinstance(item.get("doc"), dict) else {}
        record_model = str(item_doc.get("audioModelVersion") or "").strip()
        record_voice = str(item_doc.get("audioVoice") or "").strip()
        if record_model == TTS_ENGINE_VERSION and record_voice == VIENEU_DEFAULT_VOICE:
            current_records.append(item)
        else:
            stale_records.append(item)
    stale_fingerprints = {r2_record_fingerprint(item) for item in stale_records}
    remaining = [item for item in docs if r2_record_fingerprint(item) not in stale_fingerprints]
    deleted = []
    protected_paths = {
        str(path or "").strip("/")
        for item in current_records
        for path in list(item.get("objectPaths") or []) + list(item.get("orphanPaths") or [])
        if str(path or "").strip("/")
    }
    stale_paths = set()
    for item in stale_records:
        item_doc = item.get("doc") if isinstance(item.get("doc"), dict) else {}
        doc_id = str(item.get("docId") or item_doc.get("id") or "").strip()
        object_paths = []
        seen_paths = set()
        for object_key in list(item.get("objectPaths") or []) + list(item.get("orphanPaths") or []):
            object_key = str(object_key or "").strip("/")
            if object_key and object_key not in seen_paths:
                seen_paths.add(object_key)
                object_paths.append(object_key)
        stale_paths.update(object_paths)
        deleted.append({"docId": doc_id, "title": item.get("title") or item_doc.get("title") or "", "objects": len(object_paths), "bytes": int(item.get("bytes") or 0)})
    object_keys_to_delete = set(stale_paths - protected_paths)
    cleanup_entry = r2_cleanup_entry("audio-reset", ids, object_keys_to_delete, stale_records)
    if cleanup_entry:
        upsert_r2_cleanup_entry(index, cleanup_entry)
    cleanup_entries = r2_cleanup_entries_for(index, "audio-reset", ids)
    cleanup_ids = [entry.get("id") for entry in cleanup_entries]
    object_keys_to_delete.update(
        str(path or "").strip("/")
        for entry in cleanup_entries
        for path in (entry.get("objectPaths") or [])
        if str(path or "").strip("/")
    )
    object_keys_to_delete.difference_update(protected_paths)

    if not stale_records and not cleanup_entries:
        latest_result = update_r2_latest_pointer(config, index)
        latest_errors = latest_result.get("errors") or []
        return {"ok": not latest_errors, "configured": True, "provider": "r2", "deleted": [], "deleted_objects": 0, "delete_errors": latest_errors, "used_bytes": r2_cloud_index_total_bytes(index), "limit_bytes": r2_cloud_limit_bytes(config), "library_url": r2_public_url(config, "library.json")}

    # Publish both the new logical state and a durable cleanup entry before
    # deleting any stale object. A restart can therefore retry even when the
    # old record existed only in the authority file that was just replaced.
    index["docs"] = remaining
    if cleanup_ids:
        update_r2_cleanup_entries(index, cleanup_ids, object_keys_to_delete)
    write_r2_cloud_index(index)
    index = read_r2_cloud_index()
    library_url = upload_r2_cloud_library(config, index)
    latest_result = update_r2_latest_pointer(config, index)
    delete_errors = list(latest_result.get("errors") or [])
    if delete_errors:
        return {"ok": False, "configured": True, "provider": "r2", "requested": ids, "deleted": [], "pending_retry": deleted, "deleted_objects": 0, "delete_errors": delete_errors, "used_bytes": r2_cloud_index_total_bytes(index), "limit_bytes": r2_cloud_limit_bytes(config), "library_url": library_url}

    requested_paths = sorted(object_keys_to_delete)
    delete_result = r2_delete_objects(config, requested_paths)
    failed_paths = r2_failed_delete_paths(requested_paths, delete_result)
    delete_errors = list(delete_result.get("errors") or [])
    delete_ok = not failed_paths
    final_index = read_r2_cloud_index(include_deleted=True)
    if delete_ok:
        grouped = {}
        for entry in cleanup_entries:
            for doc_id, fingerprints in (entry.get("fingerprintsByDoc") or {}).items():
                grouped.setdefault(doc_id, set()).update(fingerprints)
        for record in stale_records:
            record_doc = record.get("doc") if isinstance(record.get("doc"), dict) else {}
            record_id = str(record.get("docId") or record_doc.get("id") or "").strip()
            fingerprint = r2_record_fingerprint(record)
            if record_id and fingerprint:
                grouped.setdefault(record_id, set()).add(fingerprint)
        if grouped:
            remember_r2_removed_fingerprints(grouped)
        final_index = read_r2_cloud_index(include_deleted=True)
        update_r2_cleanup_entries(final_index, cleanup_ids, remove=True)
    else:
        update_r2_cleanup_entries(final_index, cleanup_ids, failed_paths)
    write_r2_cloud_index(final_index)
    pending_summary = deleted or [{"docId": doc_id, "objects": 0, "bytes": 0} for doc_id in ids]
    return {
        "ok": delete_ok,
        "configured": True,
        "provider": "r2",
        "requested": ids,
        "deleted": deleted if delete_ok else [],
        "pending_retry": [] if delete_ok else pending_summary,
        "deleted_objects": delete_result.get("deleted_objects") or 0,
        "delete_errors": delete_errors,
        "used_bytes": r2_cached_total_bytes(),
        "limit_bytes": r2_cloud_limit_bytes(config),
        "library_url": library_url,
        "player_url": r2_public_url(config, "cloud_player.html"),
        "mobile_player_url": r2_public_url(config, "CloudMobilePlayer/index.html"),
    }


def schedule_reset_cloud_cleanup_background(store=None, delay=0.8, force=False):
    reset_ids = []
    try:
        docs = store.get("docs") if isinstance(store, dict) and isinstance(store.get("docs"), list) else []
        reset_ids = clean_doc_ids([doc.get("id") for doc in docs if isinstance(doc, dict) and doc.get("audioResetAt")])
    except Exception:
        reset_ids = []
    try:
        pending_index = read_r2_cloud_index(include_deleted=True)
        reset_ids = sorted(set(reset_ids) | {
            doc_id
            for entry in normalize_r2_cleanup_entries(pending_index.get("pendingCleanup"))
            if entry.get("kind") == "audio-reset"
            for doc_id in (entry.get("docIds") or [])
        })
    except Exception:
        pass
    if not reset_ids and not force:
        return

    global R2_RESET_CLEANUP_QUEUED
    with R2_RESET_CLEANUP_LOCK:
        if R2_RESET_CLEANUP_QUEUED:
            return
        R2_RESET_CLEANUP_QUEUED = True

    def runner():
        global R2_RESET_CLEANUP_QUEUED
        try:
            time.sleep(max(0.0, float(delay or 0)))
            retry_delay = 15
            while True:
                config = load_r2_config()
                if r2_missing_fields(config):
                    break
                try:
                    current_store = read_project_store()
                    current_ids = clean_doc_ids([
                        doc.get("id") for doc in (current_store.get("docs") or [])
                        if isinstance(doc, dict) and doc.get("audioResetAt")
                    ])
                    with CLOUD_SYNC_SERIAL_LOCK:
                        with r2_cloud_publish_lease(config, "reset-audio-cleanup"):
                            pending_index = read_r2_cloud_index(include_deleted=True)
                            durable_ids = {
                                doc_id
                                for entry in normalize_r2_cleanup_entries(pending_index.get("pendingCleanup"))
                                if entry.get("kind") == "audio-reset"
                                for doc_id in (entry.get("docIds") or [])
                            }
                            pending_ids = sorted(set(reset_ids) | set(current_ids) | durable_ids)
                            result = delete_reset_cloud_records_from_r2(pending_ids)
                    if result.get("ok"):
                        break
                except Exception:
                    pass
                time.sleep(retry_delay)
                retry_delay = min(300, retry_delay * 2)
        except Exception:
            traceback.print_exc()
        finally:
            with R2_RESET_CLEANUP_LOCK:
                R2_RESET_CLEANUP_QUEUED = False

    thread = threading.Thread(target=runner, name="r2-reset-cloud-cleanup", daemon=True)
    thread.start()


def cleanup_r2_orphan_paths():
    """Delete obsolete sync objects only after the current public library is live."""
    config = load_r2_config()
    missing = r2_missing_fields(config)
    if missing:
        return {"ok": False, "configured": False, "provider": "r2", "missing": missing, "error": "Missing R2 config fields"}
    index = read_r2_cloud_index(include_deleted=True)
    docs = index.get("docs") if isinstance(index.get("docs"), list) else []
    protected_paths = {
        str(path or "").strip("/")
        for record in docs
        for path in (record.get("objectPaths") or [])
        if str(path or "").strip("/")
    }
    orphan_paths = {
        str(path or "").strip("/")
        for record in docs
        for path in (record.get("orphanPaths") or [])
        if str(path or "").strip("/")
    }
    requested_paths = sorted(orphan_paths - protected_paths)
    if requested_paths:
        delete_result = r2_delete_objects(config, requested_paths)
        failed_paths = r2_failed_delete_paths(requested_paths, delete_result)
    else:
        delete_result = {"deleted_objects": 0, "errors": []}
        failed_paths = set()
    changed = False
    for record in docs:
        old_paths = {
            str(path or "").strip("/")
            for path in (record.get("orphanPaths") or [])
            if str(path or "").strip("/")
        }
        new_paths = sorted(old_paths & failed_paths)
        if sorted(old_paths) != new_paths:
            record["orphanPaths"] = new_paths
            changed = True
    if changed:
        index["docs"] = docs
        write_r2_cloud_index(index)
    return {
        "ok": not failed_paths,
        "configured": True,
        "provider": "r2",
        "requested_objects": len(requested_paths),
        "deleted_objects": int(delete_result.get("deleted_objects") or 0),
        "failed_paths": sorted(failed_paths),
        "delete_errors": list(delete_result.get("errors") or []),
    }


def schedule_r2_orphan_cleanup_background(delay=1.0, force=False):
    global R2_ORPHAN_CLEANUP_QUEUED
    if not force:
        try:
            index = read_r2_cloud_index(include_deleted=True)
            if not any(record.get("orphanPaths") for record in (index.get("docs") or [])):
                return
        except Exception:
            return
    with R2_ORPHAN_CLEANUP_LOCK:
        if R2_ORPHAN_CLEANUP_QUEUED:
            return
        R2_ORPHAN_CLEANUP_QUEUED = True

    def runner():
        global R2_ORPHAN_CLEANUP_QUEUED
        try:
            time.sleep(max(0.0, float(delay or 0)))
            retry_delay = 15
            while True:
                config = load_r2_config()
                if r2_missing_fields(config):
                    break
                try:
                    with CLOUD_SYNC_SERIAL_LOCK:
                        with r2_cloud_publish_lease(config, "orphan-cleanup"):
                            authority = read_r2_cloud_index(include_deleted=True)
                            remote_deleted_ids = clean_doc_ids(authority.get("deletedDocIds") or [])
                            if remote_deleted_ids:
                                remember_cloud_deleted_doc_ids(remote_deleted_ids)
                            pending = normalize_r2_cleanup_entries(authority.get("pendingCleanup"))
                            project_delete_ids = sorted({
                                doc_id
                                for entry in pending
                                if entry.get("kind") == "project-delete"
                                for doc_id in (entry.get("docIds") or [])
                            })
                            reset_ids = sorted({
                                doc_id
                                for entry in pending
                                if entry.get("kind") == "audio-reset"
                                for doc_id in (entry.get("docIds") or [])
                            })
                            result = {"ok": True}
                            if project_delete_ids:
                                result = delete_projects_from_r2(project_delete_ids)
                            if result.get("ok") and reset_ids:
                                result = delete_reset_cloud_records_from_r2(reset_ids)
                            if result.get("ok"):
                                # A prior sync-all may have committed authority but
                                # crashed before its public pointers. Repair those
                                # pointers first; only then may obsolete audio go.
                                public_index = read_r2_cloud_index()
                                upload_r2_cloud_library(config, public_index)
                                latest_result = update_r2_latest_pointer(config, public_index)
                                if latest_result.get("errors"):
                                    result = {"ok": False, "errors": latest_result.get("errors")}
                                else:
                                    result = cleanup_r2_orphan_paths()
                    if result.get("ok"):
                        break
                except Exception:
                    pass
                time.sleep(retry_delay)
                retry_delay = min(300, retry_delay * 2)
        except Exception:
            traceback.print_exc()
        finally:
            with R2_ORPHAN_CLEANUP_LOCK:
                R2_ORPHAN_CLEANUP_QUEUED = False

    threading.Thread(target=runner, name="r2-orphan-cleanup", daemon=True).start()


def sync_project_to_r2(doc_id, rebuild_library=True, upload_player_assets=True):
    doc_id = str(doc_id or "").strip()
    if cloud_doc_id_is_deleted(doc_id):
        remember_cloud_deleted_doc_ids([doc_id])
        return {"ok": True, "configured": True, "provider": "r2", "skipped": True, "deleted": True, "docId": doc_id, "message": "Project was deleted from cloud, skip R2 sync"}
    config = load_r2_config()
    missing = r2_missing_fields(config)
    if missing:
        return {"ok": False, "configured": False, "provider": "r2", "missing": missing, "error": "Missing R2 config fields"}
    store = read_project_store()
    docs = store.get("docs") if isinstance(store.get("docs"), list) else []
    doc = next((item for item in docs if item.get("id") == doc_id), None)
    if not doc:
        return {"ok": False, "configured": True, "provider": "r2", "error": "Project not found"}
    try:
        desktop_order = int(doc.get("desktopOrder"))
    except Exception:
        try:
            desktop_order = docs.index(doc)
        except Exception:
            desktop_order = 0
    manifest = doc.get("audioManifest") if isinstance(doc.get("audioManifest"), dict) else {}
    if not manifest:
        return {"ok": False, "configured": True, "provider": "r2", "error": "Project has no cached audio yet"}
    ready_audio, total_audio = project_mobile_audio_progress(doc)
    if not project_ready_for_mobile_sync(doc):
        return {"ok": False, "configured": True, "provider": "r2", "error": f"Project audio is not complete yet ({ready_audio}/{total_audio})", "ready_audio": ready_audio, "total_audio": total_audio}
    audio_jobs = doc.get("audioJobs") if isinstance(doc.get("audioJobs"), list) else []
    sync_manifest = {}
    if audio_jobs:
        for job in audio_jobs:
            if not isinstance(job, dict):
                continue
            key = str(job.get("key") or "").strip()
            value = manifest_value_for_job(manifest, key)
            if key and value:
                sync_manifest[key] = value
    else:
        sync_manifest = dict(manifest)
    previous_index = read_r2_cloud_index()
    previous_record = next((item for item in (previous_index.get("docs") or []) if item.get("docId") == doc_id), {})
    previous_object_paths = set(previous_record.get("objectPaths") or [])
    previous_cleanup_paths = previous_object_paths | set(previous_record.get("orphanPaths") or [])
    cloud_manifest = {}
    missing_audio = []
    uploaded_audio = 0
    reused_audio = 0
    uploaded_bytes = 0
    total_project_bytes = 0
    object_paths = []
    uploaded_details = []
    reused_details = []
    for cache_key, local_url in sync_manifest.items():
        name = Path(str(local_url).rsplit("/audio/", 1)[-1]).name
        if not name:
            missing_audio.append(str(local_url))
            continue
        rel = f"projects/{doc_id}/audio/{name}"
        object_key = r2_object_key(config, rel)
        if object_key in previous_object_paths:
            audio_path = resolve_audio_path(name)
            size = audio_path.stat().st_size if audio_path else 0
            reused_audio += 1
            reused_details.append({"name": name, "bytes": size})
        else:
            audio_path = resolve_audio_path(name)
            if not audio_path:
                missing_audio.append(name or str(local_url))
                continue
            size = audio_path.stat().st_size
            ctype = mimetypes.guess_type(str(audio_path))[0] or "audio/wav"
            data = audio_path.read_bytes()
            r2_put_object(config, object_key, data, ctype)
            uploaded_audio += 1
            uploaded_bytes += len(data)
            uploaded_details.append({"name": name, "bytes": len(data)})
        total_project_bytes += size
        cloud_manifest[cache_key] = r2_public_url(config, rel)
        object_paths.append(object_key)
    if missing_audio or len(cloud_manifest) < total_audio:
        return {"ok": False, "configured": True, "provider": "r2", "error": f"Project audio is missing local files ({len(cloud_manifest)}/{total_audio})", "ready_audio": len(cloud_manifest), "total_audio": total_audio, "missing_audio": missing_audio}
    audio_meta = doc_audio_metadata(doc)
    cloud_audio_items = []
    seen_audio_item_keys = set()
    for job in audio_jobs:
        if not isinstance(job, dict):
            continue
        key = str(job.get("key") or "").strip()
        text = str(job.get("text") or "").strip()
        url = cloud_manifest.get(key)
        if not key or not text or not url or key in seen_audio_item_keys:
            continue
        seen_audio_item_keys.add(key)
        cloud_audio_items.append({"key": key, "text": text, "url": url, "voice": audio_meta["audioVoice"], "voiceLabel": audio_meta["audioVoiceLabel"], "modelVersion": audio_meta["audioModelVersion"]})
    cloud_text = "\n\n".join(item.get("text") or "" for item in cloud_audio_items).strip()
    cloud_doc = {"id": doc.get("id"), "title": doc.get("title") or "File đọc", "folderPath": doc.get("folderPath") or "", "sourceName": doc.get("sourceName") or "", "text": cloud_text, "currentIndex": doc.get("currentIndex") or 0, "currentPartIndex": doc.get("currentPartIndex") or 0, "desktopOrder": desktop_order, "audioManifest": cloud_manifest, "audioItems": cloud_audio_items, "audioVoice": audio_meta["audioVoice"], "audioVoiceLabel": audio_meta["audioVoiceLabel"], "audioEngine": audio_meta["audioEngine"], "audioSpeed": doc.get("audioSpeed") or VIENEU_AUDIO_SPEED, "audioModelVersion": audio_meta["audioModelVersion"]}
    payload = {"version": 1, "syncedAt": time.strftime("%Y-%m-%dT%H:%M:%S"), "app": "Local Reader Cloud", "provider": "r2", "doc": cloud_doc, "missingAudio": missing_audio}
    manifest_rel = f"projects/{doc_id}/manifest.json"
    manifest_object_key = r2_object_key(config, manifest_rel)
    payload_bytes = json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")
    r2_put_object(config, manifest_object_key, payload_bytes, "application/json; charset=utf-8")
    object_paths.append(manifest_object_key)
    uploaded_bytes += len(payload_bytes)
    total_project_bytes += len(payload_bytes)
    if previous_record and reused_audio:
        total_project_bytes = max(total_project_bytes, int(previous_record.get("bytes") or 0))
    if upload_player_assets:
        ensure_r2_player_assets(config)
    if cloud_doc_id_is_deleted(doc_id):
        remember_cloud_deleted_doc_ids([doc_id])
        delete_result = r2_delete_objects(config, object_paths)
        index = read_r2_cloud_index()
        library_url = upload_r2_cloud_library(config, index)
        latest_result = update_r2_latest_pointer(config, index)
        errors = list(delete_result.get("errors") or []) + list(latest_result.get("errors") or [])
        return {"ok": not errors, "configured": True, "provider": "r2", "skipped": True, "deleted": not errors, "docId": doc_id, "deleted_objects": delete_result.get("deleted_objects") or 0, "delete_errors": errors, "used_bytes": r2_cached_total_bytes(), "limit_bytes": r2_cloud_limit_bytes(config), "library_url": library_url, "latest_url": latest_result.get("url") or ""}
    obsolete_paths = sorted(previous_cleanup_paths - set(object_paths))
    # Commit the new authority/library before deleting anything referenced by
    # the previous authority. Orphan paths are cleanup metadata, never reuse hits.
    record = {"docId": doc.get("id"), "title": doc.get("title") or "File đọc", "folderPath": doc.get("folderPath") or "", "desktopOrder": desktop_order, "syncedAt": time.strftime("%Y-%m-%dT%H:%M:%S"), "bytes": total_project_bytes, "audioCount": uploaded_audio + reused_audio, "uploadedAudioCount": uploaded_audio, "reusedAudioCount": reused_audio, "objectPaths": object_paths, "orphanPaths": obsolete_paths, "manifestUrl": r2_public_url(config, manifest_rel), "doc": cloud_doc}
    index, _index_deleted, library_url = update_r2_cloud_index_after_sync(config, record, rebuild_library=rebuild_library)
    r2_put_object(config, r2_object_key(config, "latest.json"), payload_bytes, "application/json; charset=utf-8")
    cleanup_deferred = not rebuild_library
    if cleanup_deferred:
        # sync-all publishes library.json only once after every record is ready;
        # keep every obsolete object until that final pointer has been committed.
        orphan_cleanup = {"deleted_objects": 0, "errors": []}
        failed_paths = set(obsolete_paths)
    else:
        orphan_cleanup = r2_delete_objects(config, obsolete_paths)
        failed_paths = r2_failed_delete_paths(obsolete_paths, orphan_cleanup)
    deleted_old = sorted(set(obsolete_paths) - failed_paths)
    if set(record.get("orphanPaths") or []) != failed_paths:
        record["orphanPaths"] = sorted(failed_paths)
        index, _ignored, _url = update_r2_cloud_index_after_sync(config, record, rebuild_library=False)
    if failed_paths and not cleanup_deferred:
        schedule_r2_orphan_cleanup_background(delay=2.0)
    return {"ok": True, "configured": True, "provider": "r2", "uploaded_audio": uploaded_audio, "reused_audio": reused_audio, "audio_count": uploaded_audio + reused_audio, "uploaded_bytes": uploaded_bytes, "project_bytes": total_project_bytes, "uploaded_details": uploaded_details[:20], "reused_details": reused_details[:20], "used_bytes": r2_cloud_index_total_bytes(index), "limit_bytes": r2_cloud_limit_bytes(config), "deleted_old": deleted_old, "cleanup_deferred": cleanup_deferred, "cleanup_pending": sorted(failed_paths), "orphan_cleanup_errors": orphan_cleanup.get("errors") or [], "missing_audio": missing_audio, "manifest_url": r2_public_url(config, manifest_rel), "latest_url": r2_public_url(config, "latest.json"), "library_url": library_url, "player_url": r2_public_url(config, "cloud_player.html"), "mobile_player_url": r2_public_url(config, "CloudMobilePlayer/index.html"), "public_base_url": str(config.get("public_base_url") or ""), "prefix": str(config.get("prefix") or "")}


def sync_all_projects_to_r2():
    config = load_r2_config()
    missing = r2_missing_fields(config)
    if missing:
        return {"ok": False, "configured": False, "provider": "r2", "missing": missing, "error": "Missing R2 config fields"}
    store = read_project_store()
    docs = store.get("docs") if isinstance(store.get("docs"), list) else []
    docs = sorted(docs, key=lambda item: item.get("updatedAt") or item.get("createdAt") or "")
    results = []
    skipped = []
    errors = []
    for doc in docs:
        try:
            manifest = doc.get("audioManifest") if isinstance(doc.get("audioManifest"), dict) else {}
            if not manifest:
                skipped.append({"docId": doc.get("id"), "title": doc.get("title"), "reason": "no_audio_manifest"})
                continue
            ready_audio, total_audio = project_mobile_audio_progress(doc)
            if not project_ready_for_mobile_sync(doc):
                skipped.append({"docId": doc.get("id"), "title": doc.get("title"), "reason": "audio_incomplete", "ready_audio": ready_audio, "total_audio": total_audio})
                continue
            result = sync_project_to_r2(doc.get("id") or "", rebuild_library=False, upload_player_assets=False)
            if not result.get("ok"):
                errors.append({"docId": doc.get("id"), "title": doc.get("title"), "error": result.get("error") or "R2 sync failed"})
                continue
            results.append({"docId": doc.get("id"), "title": doc.get("title"), "uploaded_audio": result.get("uploaded_audio"), "reused_audio": result.get("reused_audio"), "audio_count": result.get("audio_count"), "uploaded_bytes": result.get("uploaded_bytes"), "project_bytes": result.get("project_bytes"), "deleted_old": result.get("deleted_old") or [], "missing_audio": result.get("missing_audio") or []})
        except Exception as exc:
            errors.append({"docId": doc.get("id"), "title": doc.get("title"), "error": str(exc)})
    index = read_r2_cloud_index()
    library_url = upload_r2_cloud_library(config, index)
    latest_result = update_r2_latest_pointer(config, index)
    pointer_errors = list(latest_result.get("errors") or [])
    if pointer_errors:
        cleanup_result = {"ok": False, "deferred": True, "reason": "latest_pointer_failed", "delete_errors": pointer_errors}
    else:
        cleanup_result = cleanup_r2_orphan_paths()
    if not cleanup_result.get("ok"):
        schedule_r2_orphan_cleanup_background(delay=2.0, force=True)
    ensure_r2_player_assets(config)
    if pointer_errors:
        errors.append({"scope": "latest", "error": pointer_errors})
    return {"ok": not errors and cleanup_result.get("ok", False), "configured": True, "provider": "r2", "synced": results, "skipped": skipped, "errors": errors, "cleanup": cleanup_result, "used_bytes": r2_cloud_index_total_bytes(read_r2_cloud_index()), "limit_bytes": r2_cloud_limit_bytes(config), "library_url": library_url, "latest_url": latest_result.get("url") or "", "player_url": r2_public_url(config, "cloud_player.html"), "mobile_player_url": r2_public_url(config, "CloudMobilePlayer/index.html")}


def load_supabase_config():
    config = {}
    if SUPABASE_CONFIG_FILE.exists():
        try:
            config.update(json.loads(SUPABASE_CONFIG_FILE.read_text(encoding="utf-8")))
        except Exception:
            pass
    env_map = {
        "supabase_url": "SUPABASE_URL",
        "service_role_key": "SUPABASE_SERVICE_ROLE_KEY",
        "anon_key": "SUPABASE_ANON_KEY",
        "bucket": "SUPABASE_BUCKET",
        "prefix": "SUPABASE_PREFIX",
    }
    for key, env_name in env_map.items():
        if os.environ.get(env_name):
            config[key] = os.environ[env_name]
    config["prefix"] = str(config.get("prefix") or "local-reader").strip("/")
    config["bucket"] = str(config.get("bucket") or "local-reader-audio").strip()
    return config


def supabase_key(config):
    return str(config.get("service_role_key") or config.get("anon_key") or "").strip()


def supabase_missing_fields(config):
    missing = []
    if not str(config.get("supabase_url") or "").strip():
        missing.append("supabase_url")
    if not supabase_key(config):
        missing.append("service_role_key_or_anon_key")
    if not str(config.get("bucket") or "").strip():
        missing.append("bucket")
    return missing


def supabase_object_path(config, rel_path):
    rel = str(rel_path or "").replace("\\", "/").strip("/")
    prefix = str(config.get("prefix") or "").strip("/")
    return f"{prefix}/{rel}".strip("/") if prefix else rel


def supabase_public_url(config, rel_path):
    base = str(config.get("supabase_url") or "").rstrip("/")
    bucket = quote(str(config.get("bucket") or "").strip(), safe="")
    object_path = "/".join(quote(part, safe="") for part in supabase_object_path(config, rel_path).split("/"))
    return f"{base}/storage/v1/object/public/{bucket}/{object_path}"


def supabase_upload_object(config, rel_path, data, content_type):
    base = str(config.get("supabase_url") or "").rstrip("/")
    bucket = quote(str(config.get("bucket") or "").strip(), safe="")
    object_path = "/".join(quote(part, safe="") for part in supabase_object_path(config, rel_path).split("/"))
    key = supabase_key(config)
    url = f"{base}/storage/v1/object/{bucket}/{object_path}"
    request = urllib.request.Request(
        url,
        data=data,
        headers={
            "apikey": key,
            "Authorization": f"Bearer {key}",
            "Content-Type": content_type,
            "x-upsert": "true",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=120) as response:
            return response.status
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="ignore")
        raise RuntimeError(f"Supabase upload failed {exc.code}: {detail[:500]}") from exc


def supabase_delete_objects(config, object_paths):
    paths = [str(path).strip("/") for path in (object_paths or []) if str(path or "").strip("/")]
    if not paths:
        return {"deleted": 0}
    base = str(config.get("supabase_url") or "").rstrip("/")
    bucket = quote(str(config.get("bucket") or "").strip(), safe="")
    key = supabase_key(config)
    url = f"{base}/storage/v1/object/{bucket}"
    payload = json.dumps({"prefixes": paths}, ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=payload,
        headers={
            "apikey": key,
            "Authorization": f"Bearer {key}",
            "Content-Type": "application/json",
        },
        method="DELETE",
    )
    try:
        with urllib.request.urlopen(request, timeout=120) as response:
            response.read()
            return {"deleted": len(paths), "status": response.status}
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="ignore")
        raise RuntimeError(f"Supabase delete failed {exc.code}: {detail[:500]}") from exc


def read_cloud_index():
    if not CLOUD_INDEX_FILE.exists():
        return {"version": 1, "docs": []}
    try:
        data = json.loads(CLOUD_INDEX_FILE.read_text(encoding="utf-8-sig"))
        docs = data.get("docs") if isinstance(data.get("docs"), list) else []
        return {"version": 1, "docs": docs}
    except Exception:
        return {"version": 1, "docs": []}


def write_cloud_index(index):
    data = {
        "version": 1,
        "updatedAt": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "limitBytes": SUPABASE_CLOUD_LIMIT_BYTES,
        "docs": index.get("docs") if isinstance(index.get("docs"), list) else [],
    }
    tmp = CLOUD_INDEX_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(CLOUD_INDEX_FILE)
    return data


def cloud_index_total_bytes(index):
    return sum(int(item.get("bytes") or 0) for item in (index.get("docs") or []))


def prune_cloud_index(config, protected_doc_id=""):
    index = read_cloud_index()
    docs = index.get("docs") or []
    deleted = []
    while cloud_index_total_bytes({"docs": docs}) > SUPABASE_CLOUD_LIMIT_BYTES:
        candidates = [item for item in docs if item.get("docId") != protected_doc_id]
        if not candidates:
            break
        oldest = sorted(candidates, key=lambda item: item.get("syncedAt") or "")[0]
        supabase_delete_objects(config, oldest.get("objectPaths") or [])
        docs = [item for item in docs if item.get("docId") != oldest.get("docId")]
        deleted.append({
            "docId": oldest.get("docId"),
            "title": oldest.get("title"),
            "bytes": int(oldest.get("bytes") or 0),
        })
    index["docs"] = docs
    write_cloud_index(index)
    return deleted


def upload_cloud_library(config, index=None):
    index = index or read_cloud_index()
    docs = ordered_cloud_records(index)
    library_docs = [item.get("doc") for item in docs if isinstance(item.get("doc"), dict)]
    payload = {
        "version": 1,
        "updatedAt": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "app": "Local Reader Cloud Library",
        "provider": "supabase",
        "limitBytes": SUPABASE_CLOUD_LIMIT_BYTES,
        "usedBytes": cloud_index_total_bytes(index),
        "docs": library_docs,
    }
    data = json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")
    supabase_upload_object(config, "library.json", data, "application/json; charset=utf-8")
    return supabase_public_url(config, "library.json")


def update_cloud_index_after_sync(config, record):
    index = read_cloud_index()
    docs = [item for item in (index.get("docs") or []) if item.get("docId") != record.get("docId")]
    docs.append(record)
    index["docs"] = docs
    write_cloud_index(index)
    deleted = prune_cloud_index(config, protected_doc_id=record.get("docId") or "")
    index = read_cloud_index()
    library_url = upload_cloud_library(config, index)
    return index, deleted, library_url


def delete_projects_from_supabase(doc_ids):
    config = load_supabase_config()
    missing = supabase_missing_fields(config)
    if missing:
        return {"ok": False, "configured": False, "provider": "supabase", "missing": missing, "error": "Missing Supabase config fields"}
    ids = clean_doc_ids(doc_ids)
    index = read_cloud_index()
    docs = index.get("docs") if isinstance(index.get("docs"), list) else []
    if not ids:
        library_url = upload_cloud_library(config, index)
        return {"ok": True, "configured": True, "provider": "supabase", "deleted": [], "deleted_objects": 0, "delete_errors": [], "library_url": library_url, "used_bytes": cloud_index_total_bytes(index), "limit_bytes": SUPABASE_CLOUD_LIMIT_BYTES}
    target_ids = set(ids)
    remaining = []
    deleted = []
    object_paths = []
    for item in docs:
        item_doc = item.get("doc") if isinstance(item.get("doc"), dict) else {}
        doc_id = str(item.get("docId") or item_doc.get("id") or "").strip()
        if doc_id not in target_ids:
            remaining.append(item)
            continue
        paths = [str(path or "").strip("/") for path in (item.get("objectPaths") or []) if str(path or "").strip("/")]
        object_paths.extend(paths)
        deleted.append({"docId": doc_id, "title": item.get("title") or item_doc.get("title") or "", "objects": len(paths), "bytes": int(item.get("bytes") or 0)})
    delete_errors = []
    if object_paths:
        try:
            supabase_delete_objects(config, object_paths)
        except Exception as exc:
            delete_errors.append({"error": str(exc), "objects": len(object_paths)})
    index["docs"] = remaining
    index = write_cloud_index(index)
    library_url = upload_cloud_library(config, index)
    return {"ok": True, "configured": True, "provider": "supabase", "requested": ids, "deleted": deleted, "deleted_objects": len(object_paths), "delete_errors": delete_errors, "used_bytes": cloud_index_total_bytes(index), "limit_bytes": SUPABASE_CLOUD_LIMIT_BYTES, "library_url": library_url, "player_url": supabase_public_url(config, "cloud_player.html")}


def sync_project_to_supabase(doc_id):
    doc_id = str(doc_id or "").strip()
    if cloud_doc_id_is_deleted(doc_id):
        return {"ok": True, "configured": True, "provider": "supabase", "skipped": True, "deleted": True, "docId": doc_id, "message": "Project was deleted from cloud, skip Supabase sync"}
    config = load_supabase_config()
    missing = supabase_missing_fields(config)
    if missing:
        return {
            "ok": False,
            "configured": False,
            "provider": "supabase",
            "missing": missing,
            "error": "Missing Supabase config fields",
        }
    current_supabase_bytes = cloud_index_total_bytes(read_cloud_index())
    if current_supabase_bytes >= SUPABASE_CLOUD_LIMIT_BYTES:
        return {
            "ok": False,
            "configured": True,
            "provider": "supabase",
            "error": "Supabase da vuot gioi han 1 GB; hay cau hinh Cloudflare R2 de sync tiep.",
            "used_bytes": current_supabase_bytes,
            "limit_bytes": SUPABASE_CLOUD_LIMIT_BYTES,
        }
    store = read_project_store()
    docs = store.get("docs") if isinstance(store.get("docs"), list) else []
    doc = next((item for item in docs if item.get("id") == doc_id), None)
    if not doc:
        return {"ok": False, "configured": True, "provider": "supabase", "error": "Project not found"}
    try:
        desktop_order = int(doc.get("desktopOrder"))
    except Exception:
        try:
            desktop_order = docs.index(doc)
        except Exception:
            desktop_order = 0
    manifest = doc.get("audioManifest") if isinstance(doc.get("audioManifest"), dict) else {}
    if not manifest:
        return {"ok": False, "configured": True, "provider": "supabase", "error": "Project has no cached audio yet"}
    ready_audio, total_audio = project_mobile_audio_progress(doc)
    if not project_ready_for_mobile_sync(doc):
        return {"ok": False, "configured": True, "provider": "supabase", "error": f"Project audio is not complete yet ({ready_audio}/{total_audio})", "ready_audio": ready_audio, "total_audio": total_audio}

    cloud_manifest = {}
    missing_audio = []
    uploaded_audio = 0
    uploaded_bytes = 0
    object_paths = []
    for cache_key, local_url in manifest.items():
        name = Path(str(local_url).rsplit("/audio/", 1)[-1]).name
        audio_path = resolve_audio_path(name)
        if not name or not audio_path:
            missing_audio.append(name or str(local_url))
            continue
        rel = f"projects/{doc_id}/audio/{name}"
        data = audio_path.read_bytes()
        ctype = mimetypes.guess_type(str(audio_path))[0] or "audio/wav"
        supabase_upload_object(config, rel, data, ctype)
        cloud_manifest[cache_key] = supabase_public_url(config, rel)
        object_paths.append(supabase_object_path(config, rel))
        uploaded_bytes += len(data)
        uploaded_audio += 1

    if missing_audio or len(cloud_manifest) < total_audio:
        return {"ok": False, "configured": True, "provider": "supabase", "error": f"Project audio is missing local files ({len(cloud_manifest)}/{total_audio})", "ready_audio": len(cloud_manifest), "total_audio": total_audio, "missing_audio": missing_audio}

    if not cloud_manifest:
        return {"ok": False, "configured": True, "provider": "supabase", "error": "No audio file could be uploaded", "missing_audio": missing_audio}

    audio_meta = doc_audio_metadata(doc)
    audio_jobs = doc.get("audioJobs") if isinstance(doc.get("audioJobs"), list) else []
    cloud_audio_items = []
    seen_audio_item_keys = set()
    for job in audio_jobs:
        if not isinstance(job, dict):
            continue
        key = str(job.get("key") or "").strip()
        text = str(job.get("text") or "").strip()
        url = cloud_manifest.get(key)
        if not key or not text or not url or key in seen_audio_item_keys:
            continue
        seen_audio_item_keys.add(key)
        cloud_audio_items.append({"key": key, "text": text, "url": url, "voice": audio_meta["audioVoice"], "voiceLabel": audio_meta["audioVoiceLabel"], "modelVersion": audio_meta["audioModelVersion"]})
    cloud_text = "\n\n".join(item.get("text") or "" for item in cloud_audio_items).strip()
    cloud_doc = {
        "id": doc.get("id"),
        "title": doc.get("title") or "File đọc",
        "folderPath": doc.get("folderPath") or "",
        "sourceName": doc.get("sourceName") or "",
        "text": cloud_text,
        "currentIndex": doc.get("currentIndex") or 0,
        "currentPartIndex": doc.get("currentPartIndex") or 0,
        "desktopOrder": desktop_order,
        "audioManifest": cloud_manifest,
        "audioItems": cloud_audio_items,
        "audioVoice": audio_meta["audioVoice"],
        "audioVoiceLabel": audio_meta["audioVoiceLabel"],
        "audioEngine": audio_meta["audioEngine"],
        "audioSpeed": doc.get("audioSpeed") or VIENEU_AUDIO_SPEED,
        "audioModelVersion": audio_meta["audioModelVersion"],
    }
    payload = {
        "version": 1,
        "syncedAt": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "app": "Local Reader Cloud",
        "provider": "supabase",
        "doc": cloud_doc,
        "missingAudio": missing_audio,
    }
    manifest_rel = f"projects/{doc_id}/manifest.json"
    payload_bytes = json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")
    supabase_upload_object(config, manifest_rel, payload_bytes, "application/json; charset=utf-8")
    object_paths.append(supabase_object_path(config, manifest_rel))
    uploaded_bytes += len(payload_bytes)
    supabase_upload_object(config, "latest.json", payload_bytes, "application/json; charset=utf-8")

    cloud_player = ROOT / "cloud_player.html"
    if cloud_player.exists():
        supabase_upload_object(config, "cloud_player.html", cloud_player.read_bytes(), "text/html; charset=utf-8")
    if cloud_doc_id_is_deleted(doc_id):
        try:
            supabase_delete_objects(config, object_paths)
        except Exception:
            pass
        index = read_cloud_index()
        index["docs"] = [item for item in (index.get("docs") or []) if item.get("docId") != doc_id]
        index = write_cloud_index(index)
        library_url = upload_cloud_library(config, index)
        return {"ok": True, "configured": True, "provider": "supabase", "skipped": True, "deleted": True, "docId": doc_id, "deleted_objects": len(object_paths), "used_bytes": cloud_index_total_bytes(index), "limit_bytes": SUPABASE_CLOUD_LIMIT_BYTES, "library_url": library_url}
    record = {
        "docId": doc.get("id"),
        "title": doc.get("title") or "File đọc",
        "folderPath": doc.get("folderPath") or "",
        "desktopOrder": desktop_order,
        "syncedAt": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "bytes": uploaded_bytes,
        "audioCount": uploaded_audio,
        "objectPaths": object_paths,
        "manifestUrl": supabase_public_url(config, manifest_rel),
        "doc": cloud_doc,
    }
    index, deleted_old, library_url = update_cloud_index_after_sync(config, record)

    return {
        "ok": True,
        "configured": True,
        "provider": "supabase",
        "uploaded_audio": uploaded_audio,
        "audio_count": uploaded_audio,
        "uploaded_bytes": uploaded_bytes,
        "used_bytes": cloud_index_total_bytes(index),
        "limit_bytes": SUPABASE_CLOUD_LIMIT_BYTES,
        "deleted_old": deleted_old,
        "missing_audio": missing_audio,
        "manifest_url": supabase_public_url(config, manifest_rel),
        "latest_url": supabase_public_url(config, "latest.json"),
        "library_url": library_url,
        "player_url": supabase_public_url(config, "cloud_player.html"),
        "public_base_url": str(config.get("supabase_url") or ""),
        "prefix": str(config.get("prefix") or ""),
    }


def sync_all_projects_to_supabase():
    config = load_supabase_config()
    missing = supabase_missing_fields(config)
    if missing:
        return {
            "ok": False,
            "configured": False,
            "provider": "supabase",
            "missing": missing,
            "error": "Missing Supabase config fields",
        }
    store = read_project_store()
    docs = store.get("docs") if isinstance(store.get("docs"), list) else []
    docs = sorted(docs, key=lambda item: item.get("updatedAt") or item.get("createdAt") or "")
    results = []
    skipped = []
    errors = []
    for doc in docs:
        manifest = doc.get("audioManifest") if isinstance(doc.get("audioManifest"), dict) else {}
        if not manifest:
            skipped.append({"docId": doc.get("id"), "title": doc.get("title"), "reason": "no_audio_manifest"})
            continue
        ready_audio, total_audio = project_mobile_audio_progress(doc)
        if not project_ready_for_mobile_sync(doc):
            skipped.append({"docId": doc.get("id"), "title": doc.get("title"), "reason": "audio_incomplete", "ready_audio": ready_audio, "total_audio": total_audio})
            continue
        try:
            result = sync_project_to_supabase(doc.get("id") or "")
            results.append({
                "docId": doc.get("id"),
                "title": doc.get("title"),
                "uploaded_audio": result.get("uploaded_audio"),
                "audio_count": result.get("audio_count"),
                "uploaded_bytes": result.get("uploaded_bytes"),
                "deleted_old": result.get("deleted_old") or [],
            })
        except Exception as exc:
            errors.append({"docId": doc.get("id"), "title": doc.get("title"), "error": str(exc)})
    index = read_cloud_index()
    library_url = upload_cloud_library(config, index)
    return {
        "ok": not errors,
        "configured": True,
        "provider": "supabase",
        "synced": results,
        "skipped": skipped,
        "errors": errors,
        "used_bytes": cloud_index_total_bytes(index),
        "limit_bytes": SUPABASE_CLOUD_LIMIT_BYTES,
        "library_url": library_url,
        "player_url": supabase_public_url(config, "cloud_player.html"),
    }



def preferred_cloud_provider():
    return "r2" if CLOUD_ENABLED else ""


def supabase_status_payload(config=None):
    config = config or load_supabase_config()
    missing = supabase_missing_fields(config)
    return {
        "configured": not missing,
        "missing": missing,
        "used_bytes": cloud_index_total_bytes(read_cloud_index()),
        "limit_bytes": SUPABASE_CLOUD_LIMIT_BYTES,
        "library_url": supabase_public_url(config, "library.json") if not missing else "",
        "player_url": supabase_public_url(config, "cloud_player.html") if not missing else "",
    }


def r2_status_payload(config=None):
    config = config or load_r2_config()
    missing = r2_missing_fields(config)
    if not missing:
        maybe_refresh_r2_authoritative_index()
    return {
        "configured": not missing,
        "missing": missing,
        "used_bytes": r2_cached_total_bytes(),
        "limit_bytes": r2_cloud_limit_bytes(config),
        "library_url": r2_public_url(config, "library.json") if not missing else "",
        "player_url": r2_public_url(config, "cloud_player.html") if not missing else "",
        "mobile_player_url": r2_public_url(config, "CloudMobilePlayer/index.html") if not missing else "",
        "public_base_url": str(config.get("public_base_url") or ""),
        "bucket": str(config.get("bucket") or ""),
        "prefix": str(config.get("prefix") or ""),
    }


def active_cloud_limit_bytes():
    if preferred_cloud_provider() == "r2":
        return r2_cloud_limit_bytes(load_r2_config())
    return SUPABASE_CLOUD_LIMIT_BYTES


def active_cloud_used_bytes():
    if preferred_cloud_provider() == "r2":
        return r2_cached_total_bytes()
    return cloud_index_total_bytes(read_cloud_index())


def sync_project_to_cloud(doc_id):
    if not CLOUD_ENABLED:
        return cloud_disabled_result()
    doc_id = str(doc_id or "").strip()
    with CLOUD_SYNC_SERIAL_LOCK:
        if cloud_doc_id_is_deleted(doc_id):
            return {"ok": True, "configured": True, "provider": "r2", "skipped": True, "deleted": True, "docId": doc_id, "message": "Project was deleted from cloud, skip auto sync"}
        config = load_r2_config()
        missing = r2_missing_fields(config)
        if missing:
            return {"ok": False, "configured": False, "provider": "r2", "missing": missing, "error": "Missing R2 config fields"}
        try:
            with r2_cloud_publish_lease(config, f"sync:{doc_id}"):
                return sync_project_to_r2(doc_id)
        except R2CloudLeaseBusy as exc:
            return {"ok": False, "configured": True, "provider": "r2", "busy": True, "retry": True, "error": str(exc)}


def sync_all_projects_to_cloud():
    if not CLOUD_ENABLED:
        return cloud_disabled_result()
    with CLOUD_SYNC_SERIAL_LOCK:
        config = load_r2_config()
        missing = r2_missing_fields(config)
        if missing:
            return {"ok": False, "configured": False, "provider": "r2", "missing": missing, "error": "Missing R2 config fields"}
        try:
            with r2_cloud_publish_lease(config, "sync-all"):
                return sync_all_projects_to_r2()
        except R2CloudLeaseBusy as exc:
            return {"ok": False, "configured": True, "provider": "r2", "busy": True, "retry": True, "error": str(exc)}


def sync_everything_to_r2():
    if not CLOUD_ENABLED:
        return cloud_disabled_result()
    with CLOUD_SYNC_SERIAL_LOCK:
        config = load_r2_config()
        missing = r2_missing_fields(config)
        if missing:
            return {"ok": False, "configured": False, "provider": "r2", "missing": missing, "error": "Missing R2 config fields"}
        try:
            with r2_cloud_publish_lease(config, "sync-everything"):
                return sync_all_projects_to_r2()
        except R2CloudLeaseBusy as exc:
            return {"ok": False, "configured": True, "provider": "r2", "busy": True, "retry": True, "error": str(exc)}


def rebuild_cloud_library():
    if not CLOUD_ENABLED:
        return cloud_disabled_result()
    with CLOUD_SYNC_SERIAL_LOCK:
        config = load_r2_config()
        missing = r2_missing_fields(config)
        if missing:
            return {"ok": False, "configured": False, "provider": "r2", "missing": missing, "error": "Missing R2 config fields"}
        try:
            with r2_cloud_publish_lease(config, "rebuild-library"):
                index = read_r2_cloud_index()
                library_url = upload_r2_cloud_library(config, index)
                assets = ensure_r2_player_assets(config)
                return {"ok": True, "configured": True, "provider": "r2", "library_url": library_url, "assets": assets, "used_bytes": r2_cloud_index_total_bytes(index), "limit_bytes": r2_cloud_limit_bytes(config)}
        except R2CloudLeaseBusy as exc:
            return {"ok": False, "configured": True, "provider": "r2", "busy": True, "retry": True, "error": str(exc)}


def rebuild_cloud_library_background(delay=0.8):
    global CLOUD_LIBRARY_REBUILD_QUEUED
    with CLOUD_LIBRARY_REBUILD_LOCK:
        if CLOUD_LIBRARY_REBUILD_QUEUED:
            return
        CLOUD_LIBRARY_REBUILD_QUEUED = True

    def runner():
        global CLOUD_LIBRARY_REBUILD_QUEUED
        try:
            time.sleep(max(0.0, float(delay or 0)))
            rebuild_cloud_library()
        except Exception:
            traceback.print_exc()
        finally:
            with CLOUD_LIBRARY_REBUILD_LOCK:
                CLOUD_LIBRARY_REBUILD_QUEUED = False

    thread = threading.Thread(target=runner, name="cloud-library-rebuild", daemon=True)
    thread.start()


def schedule_cloud_delete_retry(doc_ids):
    global CLOUD_DELETE_RETRY_RUNNING
    ids = set(clean_doc_ids(doc_ids))
    if not ids:
        return
    with CLOUD_DELETE_RETRY_LOCK:
        CLOUD_DELETE_RETRY_IDS.update(ids)
        if CLOUD_DELETE_RETRY_RUNNING:
            return
        CLOUD_DELETE_RETRY_RUNNING = True

    def runner():
        global CLOUD_DELETE_RETRY_RUNNING
        try:
            retry_delay = 30
            while True:
                time.sleep(retry_delay)
                with CLOUD_DELETE_RETRY_LOCK:
                    pending = sorted(CLOUD_DELETE_RETRY_IDS)
                if not pending:
                    return
                try:
                    result = delete_projects_from_cloud(pending, _schedule_retry=False)
                except Exception:
                    continue
                if result.get("ok"):
                    with CLOUD_DELETE_RETRY_LOCK:
                        CLOUD_DELETE_RETRY_IDS.difference_update(pending)
                    retry_delay = 30
                else:
                    retry_delay = min(300, retry_delay * 2)
        finally:
            with CLOUD_DELETE_RETRY_LOCK:
                CLOUD_DELETE_RETRY_RUNNING = False

    threading.Thread(target=runner, name="r2-delete-retry", daemon=True).start()


def delete_projects_from_cloud(doc_ids, _schedule_retry=True):
    if not CLOUD_ENABLED:
        return cloud_disabled_result()
    with CLOUD_SYNC_SERIAL_LOCK:
        ids = clean_doc_ids(doc_ids)
        remember_cloud_deleted_doc_ids(ids)
        config = load_r2_config()
        missing = r2_missing_fields(config)
        if missing:
            if _schedule_retry:
                schedule_cloud_delete_retry(ids)
            return {"ok": False, "configured": False, "provider": "r2", "missing": missing, "pending_retry": ids, "error": "Missing R2 config fields"}
        try:
            with r2_cloud_publish_lease(config, "delete-projects"):
                result = delete_projects_from_r2(ids)
            if result.get("ok"):
                with CLOUD_DELETE_RETRY_LOCK:
                    CLOUD_DELETE_RETRY_IDS.difference_update(ids)
            if _schedule_retry and not result.get("ok"):
                schedule_cloud_delete_retry(ids)
            return result
        except R2CloudLeaseBusy as exc:
            if _schedule_retry:
                schedule_cloud_delete_retry(ids)
            return {"ok": False, "configured": True, "provider": "r2", "busy": True, "retry": True, "pending_retry": ids, "error": str(exc)}
        except Exception as exc:
            if _schedule_retry:
                schedule_cloud_delete_retry(ids)
            return {"ok": False, "configured": True, "provider": "r2", "retry": True, "pending_retry": ids, "error": str(exc)}


def cloud_status_payload():
    if not CLOUD_ENABLED:
        return cloud_disabled_result()
    r2 = r2_status_payload()
    return {
        "ok": r2["configured"],
        "configured": r2["configured"],
        "provider": "r2",
        **r2,
        "message": "" if r2["configured"] else "Cloudflare R2 chua cau hinh day du; khong ghi cloud.",
    }


def cloud_disabled_result():
    return {
        "ok": True,
        "enabled": False,
        "configured": False,
        "provider": "",
        "missing": [],
        "used_bytes": 0,
        "limit_bytes": 0,
        "skipped": True,
        "message": "Cloud sync is disabled in the local-first build. Set LOCAL_READER_CLOUD_ENABLED=1 only after configuring and reviewing cloud credentials.",
    }


def audio_url_is_cached(value):
    if not value:
        return False
    name = Path(str(value).rsplit("/audio/", 1)[-1]).name
    return bool(resolve_audio_path(name) or r2_remote_audio_url(name))


def audio_value_name(value):
    return Path(str(value or "").rsplit("/audio/", 1)[-1].split("?", 1)[0]).name


def audio_value_engine(value):
    name = audio_value_name(value).lower()
    if name.startswith("vieneu_") or "_ly_" in name:
        return "vieneu"
    return ""


def doc_audio_manifest(doc):
    return doc.get("audioManifest") if isinstance(doc, dict) and isinstance(doc.get("audioManifest"), dict) else {}


def doc_has_vieneu_audio(doc):
    return any(audio_value_engine(value) == "vieneu" for value in doc_audio_manifest(doc).values())


def doc_audio_count(doc, engine=None):
    values = list(doc_audio_manifest(doc).values())
    if engine:
        values = [value for value in values if audio_value_engine(value) == engine]
    return sum(1 for value in values if audio_url_is_cached(value))


def doc_audio_metadata(doc):
    if isinstance(doc, dict):
        locked_engine = str(doc.get("audioLockedEngine") or "").lower()
        if locked_engine == "vieneu" or doc_has_vieneu_audio(doc):
            voice = doc_audio_voice_value(doc, default="Ly")
            return {
                "audioVoice": voice,
                "audioVoiceLabel": doc_audio_voice_label_value(doc, voice, default="Ly"),
                "audioEngine": "vieneu",
                "audioModelVersion": doc.get("audioModelVersion") or VIENEU_MODEL_VERSION,
            }
    return active_tts_job_metadata()


def audio_key_hash(key):
    return str(key or "").rsplit("|", 1)[-1]


def manifest_value_for_job(manifest, job_key, engine=""):
    if not isinstance(manifest, dict):
        return ""
    value = manifest.get(job_key)
    if value and (not engine or audio_value_engine(value) == engine) and audio_url_is_cached(value):
        return value
    suffix = audio_key_hash(job_key)
    if not suffix:
        return ""
    for key, value in manifest.items():
        if audio_key_hash(key) == suffix and (not engine or audio_value_engine(value) == engine) and audio_url_is_cached(value):
            return value
    return ""


def project_mobile_audio_progress(doc):
    if not isinstance(doc, dict):
        return 0, 0
    manifest = doc_audio_manifest(doc)
    jobs = doc.get("audioJobs") if isinstance(doc.get("audioJobs"), list) else []
    chunks = doc.get("chunks") if isinstance(doc.get("chunks"), list) else []
    rich_chunks = doc.get("richChunks") if isinstance(doc.get("richChunks"), list) else []
    audio_items = doc.get("audioItems") if isinstance(doc.get("audioItems"), list) else []
    try:
        chunk_count = int(doc.get("chunkCount") or 0)
    except Exception:
        chunk_count = 0
    expected_chunks = len(rich_chunks) or chunk_count or len(chunks)
    if expected_chunks <= 0 and str(doc.get("text") or "").strip():
        expected_chunks = len(split_project_doc_text(doc.get("text") or ""))
    if jobs:
        ready = 0
        seen = set()
        for job in jobs:
            if not isinstance(job, dict):
                continue
            key = str(job.get("key") or "").strip()
            if not key or key in seen:
                continue
            seen.add(key)
            if manifest_value_for_job(manifest, key):
                ready += 1
        return ready, max(expected_chunks, len(audio_items), len(seen))
    expected = max(expected_chunks, len(audio_items))
    ready_keys = set()
    for key, value in manifest.items():
        if key and audio_url_is_cached(value):
            ready_keys.add(str(key))
    if audio_items:
        for item in audio_items:
            if not isinstance(item, dict):
                continue
            key = str(item.get("key") or "").strip()
            if key and manifest_value_for_job(manifest, key):
                ready_keys.add(key)
    if expected <= 0:
        expected = len(ready_keys)
    return len(ready_keys), expected


def project_ready_for_mobile_sync(doc):
    ready, expected = project_mobile_audio_progress(doc)
    return expected > 0 and ready >= expected


def sanitize_audio_entries(*manifests, engine="", jobs=None, audio_items=None):
    entries = []
    seen_keys = set()
    seen_hashes = set()
    merged = {}
    for manifest in manifests:
        if not isinstance(manifest, dict):
            continue
        merged.update(manifest)

    def entry_seen(key):
        suffix = audio_key_hash(key)
        return key in seen_keys or (suffix and suffix in seen_hashes)

    def mark_entry(key):
        seen_keys.add(key)
        suffix = audio_key_hash(key)
        if suffix:
            seen_hashes.add(suffix)

    def key_matches_engine_version(key):
        return True

    for item in audio_items if isinstance(audio_items, list) else []:
        if not isinstance(item, dict):
            continue
        key = str(item.get("key") or "").strip() or f"audio-item-{len(entries) + 1:03d}"
        if not key_matches_engine_version(key):
            continue
        value = item.get("url") or merged.get(key)
        if engine and audio_value_engine(value) != engine:
            continue
        name = audio_value_name(value)
        if not name or entry_seen(key) or not audio_url_is_cached(value):
            continue
        mark_entry(key)
        entries.append((key, audio_url(name), str(item.get("text") or "").strip()))

    for job in jobs if isinstance(jobs, list) else []:
        if not isinstance(job, dict):
            continue
        key = str(job.get("key") or "").strip()
        if not key or entry_seen(key) or not key_matches_engine_version(key):
            continue
        value = manifest_value_for_job(merged, key, engine=engine)
        name = audio_value_name(value)
        if not name:
            continue
        mark_entry(key)
        entries.append((key, audio_url(name), str(job.get("text") or "").strip()))

    for key, value in merged.items():
        if engine and audio_value_engine(value) != engine:
            continue
        key = str(key)
        name = audio_value_name(value)
        if not name or entry_seen(key) or not key_matches_engine_version(key):
            continue
        if not audio_url_is_cached(value):
            continue
        mark_entry(key)
        entries.append((key, audio_url(name), ""))
    return entries


PROJECT_AUDIO_CHUNK_MIN_CHARS = 450
PROJECT_AUDIO_CHUNK_MAX_CHARS = 920


def normalize_project_chunk_text(text):
    return re.sub(r"\s+", " ", str(text or "").replace("\r", " ").replace("\n", " ")).strip()


def ensure_project_chunk_punctuation(text):
    value = normalize_project_chunk_text(text)
    if not value:
        return ""
    return value if re.search(r"[.!?;:,)]$", value) else value + "."


def split_long_project_chunk(text, max_len=PROJECT_AUDIO_CHUNK_MAX_CHARS):
    rest = normalize_project_chunk_text(text)
    if not rest:
        return []
    if len(rest) <= max_len:
        return [ensure_project_chunk_punctuation(rest)]
    pieces = []
    while len(rest) > max_len:
        cut = -1
        head = rest[:max_len + 1]
        for pattern in (r"[.!?]\s+", r"[;:]\s+", r",\s+"):
            for match in re.finditer(pattern, head):
                if match.start() >= PROJECT_AUDIO_CHUNK_MIN_CHARS:
                    cut = match.end()
            if cut >= PROJECT_AUDIO_CHUNK_MIN_CHARS:
                break
        if cut < PROJECT_AUDIO_CHUNK_MIN_CHARS:
            cut = rest.rfind(" ", 0, max_len)
            if cut < PROJECT_AUDIO_CHUNK_MIN_CHARS:
                cut = max_len
        pieces.append(ensure_project_chunk_punctuation(rest[:cut]))
        rest = rest[cut:].strip()
    if rest:
        pieces.append(ensure_project_chunk_punctuation(rest))
    return pieces


def split_project_doc_text(text):
    cleaned = str(text or "").replace("\r\n", "\n").replace("\r", "\n")
    paragraphs = [ensure_project_chunk_punctuation(part) for part in re.split(r"\n{2,}", cleaned) if normalize_project_chunk_text(part)]
    chunks = []
    buffer = ""

    def flush():
        nonlocal buffer
        if buffer:
            chunks.extend(split_long_project_chunk(buffer))
        buffer = ""

    for paragraph in paragraphs:
        if len(paragraph) > PROJECT_AUDIO_CHUNK_MAX_CHARS:
            flush()
            chunks.extend(split_long_project_chunk(paragraph))
            continue
        if not buffer:
            buffer = paragraph
            continue
        candidate = normalize_project_chunk_text(f"{buffer} {paragraph}")
        if len(buffer) < PROJECT_AUDIO_CHUNK_MIN_CHARS and len(candidate) <= PROJECT_AUDIO_CHUNK_MAX_CHARS:
            buffer = candidate
        else:
            flush()
            buffer = paragraph
    flush()
    return chunks


def base36(value):
    value = int(value) & 0xFFFFFFFF
    chars = "0123456789abcdefghijklmnopqrstuvwxyz"
    if value == 0:
        return "0"
    output = ""
    while value:
        value, rem = divmod(value, 36)
        output = chars[rem] + output
    return output


def stable_hash_text(text):
    def u32(value):
        return int(value) & 0xFFFFFFFF

    def imul(a, b):
        return u32(u32(a) * u32(b))

    h1 = 0xDEADBEEF
    h2 = 0x41C6CE57
    raw = str(text or "").encode("utf-16-le", "surrogatepass")
    for index in range(0, len(raw), 2):
        code = int.from_bytes(raw[index:index + 2], "little")
        h1 = imul(h1 ^ code, 2654435761)
        h2 = imul(h2 ^ code, 1597334677)
    h1 = u32(imul(h1 ^ (h1 >> 16), 2246822507) ^ imul(h2 ^ (h2 >> 13), 3266489909))
    h2 = u32(imul(h2 ^ (h2 >> 16), 2246822507) ^ imul(h1 ^ (h1 >> 13), 3266489909))
    return base36(h2) + base36(h1)


def project_audio_cache_key(text, voice=None):
    clean = prepare_tts_text(text)
    return f"vieneu|{TTS_ENGINE_VERSION}|{VIENEU_AUDIO_SPEED}|{voice or VIENEU_DEFAULT_VOICE}|{stable_hash_text(clean)}"


def project_audio_signature_from_jobs(jobs):
    keys = [str(job.get("key") or "").strip() for job in jobs if isinstance(job, dict) and str(job.get("key") or "").strip()]
    payload = json.dumps({
        "engine": "vieneu",
        "modelVersion": TTS_ENGINE_VERSION,
        "speed": VIENEU_AUDIO_SPEED,
        "voice": VIENEU_DEFAULT_VOICE,
        "keys": keys,
    }, ensure_ascii=False, separators=(",", ":"))
    return stable_hash_text(payload)


def project_chunks_for_audio(doc):
    rich_chunks = doc.get("richChunks") if isinstance(doc, dict) and isinstance(doc.get("richChunks"), list) else []
    chunks = []
    for item in rich_chunks:
        if isinstance(item, dict):
            text = ensure_project_chunk_punctuation(item.get("text") or "")
        else:
            text = ensure_project_chunk_punctuation(item)
        if text:
            chunks.append(text)
    if chunks:
        return chunks
    return split_project_doc_text((doc.get("text") or "") if isinstance(doc, dict) else "")


def build_project_prepare_jobs_for_doc(doc):
    if not isinstance(doc, dict):
        return []
    jobs = []
    for chunk in project_chunks_for_audio(doc):
        text = prepare_tts_text(chunk)
        if not text:
            continue
        voice = VIENEU_DEFAULT_VOICE
        jobs.append({
            "key": project_audio_cache_key(text, voice),
            "text": text,
        "voice": voice,
            "speed": VIENEU_AUDIO_SPEED,
            "modelVersion": TTS_ENGINE_VERSION,
        })
    return jobs


def sanitize_project_audio_items(doc, entries, voice, voice_label, model_version):
    jobs = []
    items = []
    fallback_chunks = split_project_doc_text(doc.get("text") or "")
    preferred_texts = preferred_audio_texts_by_hash(doc.get("audioItems"), doc.get("audioJobs"))
    for index, entry in enumerate(entries):
        key, url = entry[0], entry[1]
        text = str(entry[2] if len(entry) > 2 else "").strip()
        if audio_text_is_generic(text):
            text = preferred_texts.get(audio_key_hash(key)) or (fallback_chunks[index] if index < len(fallback_chunks) else "")
        if audio_text_is_generic(text):
            text = f"Đoạn {index + 1}"
        jobs.append({"key": key, "text": text, "voice": voice, "speed": VIENEU_AUDIO_SPEED, "modelVersion": model_version})
        items.append({"key": key, "text": text, "url": url, "voice": voice, "voiceLabel": voice_label, "modelVersion": model_version})
    existing_jobs = doc.get("audioJobs") if isinstance(doc.get("audioJobs"), list) else []
    merged_jobs = merge_audio_records(jobs, existing_jobs, preferred_texts)
    doc["audioJobs"] = merged_jobs if len(merged_jobs) >= len(jobs) else jobs
    doc["audioItems"] = items


def audio_record_hash(record):
    if not isinstance(record, dict):
        return ""
    key = str(record.get("key") or "").strip()
    if key:
        return audio_key_hash(key)
    url = str(record.get("url") or "").strip()
    return audio_value_name(url)


def audio_text_is_generic(text):
    text = str(text or "").strip()
    if not text:
        return True
    return bool(re.fullmatch(r"(?:Đoạn|Doan)\s+\d+", text, flags=re.IGNORECASE))


def preferred_audio_texts_by_hash(*record_lists):
    preferred = {}
    for records in record_lists:
        if not isinstance(records, list):
            continue
        for record in records:
            suffix = audio_record_hash(record)
            text = str(record.get("text") or "").strip() if isinstance(record, dict) else ""
            if suffix and text and not audio_text_is_generic(text):
                preferred[suffix] = text
    return preferred


def merge_audio_records(existing_records, incoming_records, preferred_texts):
    output = []
    seen_hashes = set()

    def add_records(records):
        if not isinstance(records, list):
            return
        for record in records:
            if not isinstance(record, dict):
                continue
            suffix = audio_record_hash(record)
            if not suffix or suffix in seen_hashes:
                continue
            item = dict(record)
            text = str(item.get("text") or "").strip()
            if audio_text_is_generic(text) and preferred_texts.get(suffix):
                item["text"] = preferred_texts[suffix]
            output.append(item)
            seen_hashes.add(suffix)

    add_records(incoming_records)
    add_records(existing_records)
    return output


def should_accept_content_update(existing, incoming):
    if not isinstance(existing, dict) or not existing.get("id"):
        return True
    if (
        not str(incoming.get("text") or "").strip()
        and str(existing.get("text") or "").strip()
    ):
        return False
    incoming_marker = bounded_client_timestamp(incoming.get("contentEditedAt"))
    if not incoming_marker:
        return False
    existing_marker = bounded_client_timestamp(existing.get("contentEditedAt"))
    return not existing_marker or incoming_marker >= existing_marker


def should_accept_layout_update(existing, incoming):
    if not isinstance(existing, dict) or not existing.get("id"):
        return True
    incoming_marker = bounded_client_timestamp(incoming.get("layoutUpdatedAt"))
    existing_marker = bounded_client_timestamp(existing.get("layoutUpdatedAt"))
    if existing_marker and not incoming_marker:
        return False
    return not existing_marker or incoming_marker >= existing_marker


def safe_audio_cache_component(value, limit=36):
    return re.sub(r"[^A-Za-z0-9_-]+", "_", str(value or ""))[:limit]


def active_vieneu_cache_prefix(model_version=None, speed=None, voice=None):
    safe_model = safe_audio_cache_component(model_version or TTS_ENGINE_VERSION, 36)
    safe_voice = safe_audio_cache_component(voice or VIENEU_DEFAULT_VOICE, 32)
    return f"vieneu_{safe_model}_{speed or VIENEU_AUDIO_SPEED}_{safe_voice}_".lower()


def audio_key_matches_active_tts(key):
    parts = str(key or "").split("|")
    return (
        len(parts) >= 5
        and parts[0] == "vieneu"
        and parts[1] == TTS_ENGINE_VERSION
        and parts[2] == VIENEU_AUDIO_SPEED
        and parts[3] == VIENEU_DEFAULT_VOICE
    )


def audio_value_matches_active_tts(value):
    name = audio_value_name(value).lower()
    return bool(name and name.startswith(active_vieneu_cache_prefix()))


def filter_manifest_for_active_tts(manifest):
    if not isinstance(manifest, dict):
        return {}
    return {
        str(key): value
        for key, value in manifest.items()
        if audio_key_matches_active_tts(key)
        and audio_value_matches_active_tts(value)
        and audio_url_is_cached(value)
    }


def audio_record_matches_active_tts(record, require_url=False):
    if not isinstance(record, dict):
        return False
    key = str(record.get("key") or "").strip()
    voice = str(record.get("voice") or "").strip()
    model_version = str(record.get("modelVersion") or "").strip()
    if voice and voice != VIENEU_DEFAULT_VOICE:
        return False
    if model_version and model_version != TTS_ENGINE_VERSION:
        return False
    if key and not audio_key_matches_active_tts(key):
        return False
    url = record.get("url") or ""
    if url:
        return audio_value_matches_active_tts(url) and audio_url_is_cached(url)
    return not require_url and bool(key or str(record.get("text") or "").strip())


def filter_audio_records_for_active_tts(records, require_url=False):
    if not isinstance(records, list):
        return []
    return [
        record
        for record in records
        if audio_record_matches_active_tts(record, require_url=require_url)
    ]


def sanitize_project_store_for_write(data):
    existing_docs = {}
    try:
        existing_data = read_project_store()
        existing_docs = {
            str(doc.get("id") or ""): doc
            for doc in existing_data.get("docs", [])
            if isinstance(doc, dict) and doc.get("id")
        }
    except Exception:
        existing_docs = {}

    docs = []
    incoming_ids = set()
    deleted_ids = set(clean_doc_ids(data.get("deletedDocIds") if isinstance(data, dict) else []))
    all_deleted_ids = read_cloud_deleted_doc_ids() | deleted_ids
    for incoming in data.get("docs") if isinstance(data.get("docs"), list) else []:
        if not isinstance(incoming, dict):
            continue
        doc = normalize_project_doc_timestamps(dict(incoming))
        doc_id = str(doc.get("id") or "").strip()
        if doc_id:
            incoming_ids.add(doc_id)
        if doc_id and doc_id in all_deleted_ids:
            continue
        existing = existing_docs.get(doc_id) or {}
        if existing and progress_timestamp(existing) > progress_timestamp(doc):
            copy_progress_fields(doc, existing)
        if (
            not existing
            and doc.get("serverBacked")
            and not str(doc.get("text") or "").strip()
        ):
            continue
        if existing and not should_accept_content_update(existing, doc):
            for field in ("title", "text", "chunkProfile", "chunkCount", "richChunks", "chunks"):
                if field in existing:
                    doc[field] = existing[field]
            if existing.get("contentEditedAt"):
                doc["contentEditedAt"] = existing.get("contentEditedAt")
        if existing and not should_accept_layout_update(existing, doc):
            for field in ("desktopOrder", "folderPath", "collection", "layoutUpdatedAt"):
                if field in existing:
                    doc[field] = existing[field]
        try:
            safe_chunk_count = int(doc.get("chunkCount") or 0)
        except Exception:
            safe_chunk_count = 0
        if str(doc.get("text") or "").strip() and safe_chunk_count <= 0:
            doc["chunkCount"] = len(split_project_doc_text(doc.get("text") or ""))
        existing_manifest = existing.get("audioManifest") if isinstance(existing.get("audioManifest"), dict) else {}
        incoming_manifest = doc.get("audioManifest") if isinstance(doc.get("audioManifest"), dict) else {}
        incoming_items = doc.get("audioItems") if isinstance(doc.get("audioItems"), list) else []
        existing_items = existing.get("audioItems") if isinstance(existing.get("audioItems"), list) else []
        incoming_jobs = doc.get("audioJobs") if isinstance(doc.get("audioJobs"), list) else []
        existing_jobs = existing.get("audioJobs") if isinstance(existing.get("audioJobs"), list) else []
        audio_reset_at = str(doc.get("audioResetAt") or existing.get("audioResetAt") or "").strip()
        if audio_reset_at:
            doc["audioResetAt"] = audio_reset_at
            existing_manifest = filter_manifest_for_active_tts(existing_manifest)
            incoming_manifest = filter_manifest_for_active_tts(incoming_manifest)
            existing_items = filter_audio_records_for_active_tts(existing_items, require_url=True)
            incoming_items = filter_audio_records_for_active_tts(incoming_items, require_url=True)
            existing_jobs = filter_audio_records_for_active_tts(existing_jobs)
            incoming_jobs = filter_audio_records_for_active_tts(incoming_jobs)
            doc["audioVoice"] = VIENEU_DEFAULT_VOICE
            doc["audioVoiceLabel"] = VIENEU_DEFAULT_VOICE_LABEL
            doc["audioModelVersion"] = TTS_ENGINE_VERSION
        preferred_texts = preferred_audio_texts_by_hash(existing_items, existing_jobs, incoming_items, incoming_jobs)
        audio_items = merge_audio_records(existing_items, incoming_items, preferred_texts)
        audio_jobs = merge_audio_records(existing_jobs, incoming_jobs, preferred_texts)
        vieneu_entries = sanitize_audio_entries(existing_manifest, incoming_manifest, engine="vieneu", jobs=audio_jobs, audio_items=audio_items)
        if vieneu_entries:
            generated_jobs = build_project_prepare_jobs_for_doc(doc)
            generated_keys = {str(job.get("key") or "").strip() for job in generated_jobs}
            generated_hashes = {audio_key_hash(key) for key in generated_keys if audio_key_hash(key)}
            ready_keys = {str(entry[0]) for entry in vieneu_entries}
            ready_hashes = {audio_key_hash(key) for key in ready_keys if audio_key_hash(key)}
            if generated_jobs and generated_hashes:
                entry_by_hash = {
                    audio_key_hash(entry[0]): entry
                    for entry in vieneu_entries
                    if audio_key_hash(entry[0])
                }
                normalized_entries = []
                used_hashes = set()
                for job in generated_jobs:
                    key = str(job.get("key") or "").strip()
                    suffix = audio_key_hash(key)
                    entry = entry_by_hash.get(suffix)
                    if entry:
                        normalized_entries.append((key, entry[1], str(job.get("text") or entry[2] or "").strip()))
                        used_hashes.add(suffix)
                normalized_entries.extend(
                    entry
                    for entry in vieneu_entries
                    if audio_key_hash(entry[0]) not in used_hashes
                )
                vieneu_entries = normalized_entries
            voice_for_doc = doc_audio_voice_value(doc, default="Ly")
            voice_label_for_doc = doc_audio_voice_label_value(doc, voice_for_doc, default="Ly")
            doc["audioManifest"] = {entry[0]: entry[1] for entry in vieneu_entries}
            doc["audioVoice"] = voice_for_doc
            doc["audioVoiceLabel"] = voice_label_for_doc
            doc["audioEngine"] = "vieneu"
            doc.pop("audioLockedEngine", None)
            doc["audioSpeed"] = VIENEU_AUDIO_SPEED
            doc["audioModelVersion"] = doc.get("audioModelVersion") or VIENEU_MODEL_VERSION
            all_generated_ready = bool(
                generated_jobs
                and (
                    generated_keys.issubset(ready_keys)
                    or (generated_hashes and generated_hashes.issubset(ready_hashes))
                )
            )
            if all_generated_ready:
                doc["audioSignature"] = project_audio_signature_from_jobs(generated_jobs)
                doc["chunkCount"] = len(generated_jobs)
                doc["audioJobs"] = generated_jobs
            else:
                doc.pop("audioSignature", None)
            doc["prepareRequested"] = False
            sanitize_project_audio_items(doc, vieneu_entries, voice_for_doc, voice_label_for_doc, doc.get("audioModelVersion") or VIENEU_MODEL_VERSION)
            docs.append(doc)
            continue

        doc["audioManifest"] = {}
        doc["audioItems"] = []
        doc["audioVoice"] = active_tts_audio_voice()
        doc["audioVoiceLabel"] = active_tts_audio_voice_label()
        doc["audioEngine"] = "vieneu"
        doc.pop("audioLockedEngine", None)
        doc["audioModelVersion"] = TTS_ENGINE_VERSION
        doc.pop("audioSignature", None)
        doc["prepareRequested"] = False
        docs.append(doc)

    kept_ids = {str(doc.get("id") or "").strip() for doc in docs if isinstance(doc, dict)}
    for existing_id, existing in existing_docs.items():
        if (
            existing_id
            and existing_id not in kept_ids
            and existing_id not in incoming_ids
            and existing_id not in all_deleted_ids
        ):
            docs.append(existing)
            kept_ids.add(existing_id)

    data["docs"] = docs
    return data


def merge_project_doc(doc, active_doc_id=""):
    if not isinstance(doc, dict) or not doc.get("id"):
        raise ValueError("Project doc id is required")
    with PROJECT_LOCK:
        store = read_project_store()
        docs = store.get("docs") if isinstance(store.get("docs"), list) else []
        existing_index = next((i for i, item in enumerate(docs) if item.get("id") == doc.get("id")), -1)
        merged = dict(doc)
        if existing_index >= 0:
            existing = docs[existing_index]
            existing_manifest = existing.get("audioManifest") if isinstance(existing.get("audioManifest"), dict) else {}
            new_manifest = merged.get("audioManifest") if isinstance(merged.get("audioManifest"), dict) else {}
            merged = {**existing, **merged}
            merged["audioManifest"] = {**existing_manifest, **new_manifest}
            docs[existing_index] = merged
        else:
            docs.append(merged)
        store["docs"] = docs
        effective_active_doc_id = active_doc_id or store.get("activeDocId") or merged.get("id")
        patch_result = write_project_patch(merged, effective_active_doc_id)
        return patch_result, patch_result.get("doc") or merged


def project_patch_path(doc_id):
    digest = hashlib.sha256(str(doc_id or "").encode("utf-8")).hexdigest()
    return PROJECT_PATCH_DIR / current_device_id() / f"{digest}.json"


def write_project_patch(doc, active_doc_id=""):
    """Persist one edited document without rewriting the ~30 MB device snapshot."""
    global PROJECT_CACHE_SIGNATURE, PROJECT_CACHE_STORE
    if not isinstance(doc, dict) or not str(doc.get("id") or "").strip():
        raise ValueError("Project doc id is required")
    doc_id = str(doc.get("id") or "").strip()
    with PROJECT_LOCK:
        store = read_project_store()
        existing = next(
            (item for item in store.get("docs", []) if isinstance(item, dict) and str(item.get("id") or "") == doc_id),
            {},
        )
        incoming = dict(doc)
        existing_manifest = existing.get("audioManifest") if isinstance(existing.get("audioManifest"), dict) else {}
        incoming_manifest = incoming.get("audioManifest") if isinstance(incoming.get("audioManifest"), dict) else {}
        merged = {**existing, **incoming}
        merged["id"] = doc_id
        merged["audioManifest"] = {**existing_manifest, **incoming_manifest}

        # Reuse the same validation/content-conflict rules as a full snapshot write,
        # then keep only this document in a compact per-device delta file.
        sanitized_store = sanitize_project_store_for_write({
            "docs": [merged],
            "activeDocId": active_doc_id or store.get("activeDocId") or doc_id,
            "deletedDocIds": [],
        })
        sanitized = next(
            (item for item in sanitized_store.get("docs", []) if isinstance(item, dict) and str(item.get("id") or "") == doc_id),
            None,
        )
        if not sanitized:
            raise ValueError("Project doc was rejected")

        now_iso = datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")
        patch_payload = {
            "version": 3,
            "kind": "project-doc-delta",
            "deviceId": current_device_id(),
            "deviceName": os.environ.get("COMPUTERNAME") or "device",
            "docs": [sanitized],
            "activeDocId": active_doc_id or store.get("activeDocId") or doc_id,
            "savedAt": now_iso,
        }
        target = project_patch_path(doc_id)
        target.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_text(target, json.dumps(patch_payload, ensure_ascii=False, indent=2))
        PROJECT_CACHE_SIGNATURE = None
        PROJECT_CACHE_STORE = None
        set_path_hidden(PROJECT_PATCH_DIR)
        set_path_hidden(target.parent)
        set_path_hidden(target)
        return {"ok": True, "doc": sanitized, "activeDocId": patch_payload["activeDocId"], "savedAt": now_iso}



def update_project_audio(doc_id, manifest_updates=None, audio_signature=None):
    with PROJECT_LOCK:
        store = read_project_store()
        docs = store.get("docs") if isinstance(store.get("docs"), list) else []
        for doc in docs:
            if doc.get("id") != doc_id:
                continue
            manifest = doc.get("audioManifest") if isinstance(doc.get("audioManifest"), dict) else {}
            manifest.update(manifest_updates or {})
            doc["audioManifest"] = manifest
            doc["audioVoice"] = active_tts_audio_voice()
            doc["audioVoiceLabel"] = active_tts_audio_voice_label()
            doc["audioEngine"] = active_tts_audio_engine()
            doc["audioSpeed"] = VIENEU_AUDIO_SPEED
            doc["audioModelVersion"] = TTS_ENGINE_VERSION
            audio_now = datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")
            if audio_signature:
                doc["audioSignature"] = audio_signature
                doc["prepareRequested"] = False
                doc["audioPreparedAt"] = audio_now
            doc["audioUpdatedAt"] = audio_now
            patch_result = write_project_patch(doc, store.get("activeDocId") or doc_id)
            return patch_result.get("doc") or doc
    return None


def mark_project_vieneu_locked(doc_id):
    with PROJECT_LOCK:
        store = read_project_store()
        docs = store.get("docs") if isinstance(store.get("docs"), list) else []
        for doc in docs:
            if doc.get("id") != doc_id:
                continue
            doc["audioVoice"] = "Ly"
            doc["audioVoiceLabel"] = "Ly [VieNeu]"
            doc["audioEngine"] = "vieneu"
            doc["audioLockedEngine"] = "vieneu"
            doc["audioModelVersion"] = VIENEU_MODEL_VERSION
            doc["prepareRequested"] = False
            doc["audioUpdatedAt"] = datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")
            patch_result = write_project_patch(doc, store.get("activeDocId") or doc_id)
            return patch_result.get("doc") or doc
    return None


def job_snapshot(doc_id=None):
    with BACKGROUND_LOCK:
        if doc_id:
            job = BACKGROUND_JOBS.get(doc_id)
            return job_progress_fields(dict(job)) if isinstance(job, dict) else None
        return {key: job_progress_fields(dict(value)) for key, value in BACKGROUND_JOBS.items()}



def job_progress_fields(job):
    now = time.time()
    started_ts = job.get("startedTs") or now
    try:
        started_ts = float(started_ts)
    except Exception:
        started_ts = now
    done = int(job.get("done") or 0)
    total = int(job.get("total") or 0)
    elapsed = max(0.0, now - started_ts)
    eta = None
    if done > 0 and total > done:
        eta = max(0.0, elapsed / done * (total - done))
    job["elapsedSeconds"] = round(elapsed, 1)
    job["etaSeconds"] = round(eta, 1) if eta is not None else None
    job["percent"] = round(done / total * 100, 1) if total else 0
    return job


def set_job_state(doc_id, **updates):
    with BACKGROUND_LOCK:
        job = BACKGROUND_JOBS.setdefault(doc_id, {})
        if "status" in updates and updates.get("status") == "running" and not job.get("startedTs"):
            job["startedTs"] = time.time()
            job["startedAt"] = time.strftime("%Y-%m-%dT%H:%M:%S")
        job.update(updates)
        job["updatedAt"] = time.strftime("%Y-%m-%dT%H:%M:%S")
        return job_progress_fields(dict(job))



def normalize_prepare_jobs(jobs):
    seen = set()
    output = []
    for job in jobs if isinstance(jobs, list) else []:
        if not isinstance(job, dict):
            continue
        raw_text = job.get("text") or ""
        text = prepare_tts_text(raw_text)
        if not text:
            continue
        voice = str(job.get("voice") or VIENEU_DEFAULT_VOICE).strip() or VIENEU_DEFAULT_VOICE
        speed = str(job.get("speed") or VIENEU_AUDIO_SPEED).strip() or VIENEU_AUDIO_SPEED
        model_version = str(job.get("modelVersion") or TTS_ENGINE_VERSION).strip() or TTS_ENGINE_VERSION
        key = str(job.get("key") or "").strip()
        if not key:
            key_payload = json.dumps({
                "engine": "vieneu",
                "voice": voice,
                "speed": speed,
                "model_version": model_version,
                "text": text,
            }, ensure_ascii=False, sort_keys=True)
            key = hashlib.sha256(key_payload.encode("utf-8")).hexdigest()[:32]
        if key in seen:
            continue
        seen.add(key)
        output.append({
            "key": key,
            "text": text,
            "voice": voice,
            "speed": speed,
            "modelVersion": model_version,
        })
    return output


def cached_audio_url_for_job(job, manifest=None):
    if not isinstance(job, dict):
        return ""
    manifest = manifest if isinstance(manifest, dict) else {}
    key = str(job.get("key") or "").strip()
    value = manifest_value_for_job(manifest, key) if key else ""
    if value:
        return value
    text = prepare_tts_text(job.get("text") or "")
    if not text:
        return ""
    voice = normalize_tts_voice(job.get("voice") or VIENEU_DEFAULT_VOICE)
    filenames = [vieneu_cache_filename(text, voice)]
    if ALLOW_LEGACY_VIENEU_CACHE:
        filenames.append(legacy_vieneu_cache_filename(text, voice, job.get("modelVersion") or TTS_ENGINE_VERSION, job.get("speed") or VIENEU_AUDIO_SPEED))
    for filename in filenames:
        if resolve_audio_path(filename) or r2_remote_audio_url(filename):
            return audio_url(filename)
    return ""


def cached_job_count(jobs, manifest):
    manifest = manifest if isinstance(manifest, dict) else {}
    return sum(1 for job in jobs if cached_audio_url_for_job(job, manifest))



def auto_sync_project_to_cloud_background(doc_id):
    doc_id = str(doc_id or "").strip()
    if not doc_id:
        return
    if not CLOUD_ENABLED:
        set_job_state(
            doc_id,
            cloudSyncStatus="disabled",
            cloudSyncMessage="Cloud sync is disabled in the local-first build",
            cloudLimitBytes=0,
        )
        return
    if preferred_cloud_provider() != "r2":
        set_job_state(
            doc_id,
            cloudSyncStatus="disabled",
            cloudSyncMessage="Cloudflare R2 chua cau hinh, tam tat auto sync cloud",
            cloudLimitBytes=active_cloud_limit_bytes(),
        )
        return
    with CLOUD_AUTO_SYNC_LOCK:
        if doc_id in CLOUD_AUTO_SYNC_IN_FLIGHT:
            return
        CLOUD_AUTO_SYNC_IN_FLIGHT.add(doc_id)

    def runner():
        provider = preferred_cloud_provider().upper()
        try:
            set_job_state(doc_id, cloudSyncStatus="running", cloudSyncMessage=f"Dang auto sync {provider}", cloudLimitBytes=active_cloud_limit_bytes())
            result = sync_project_to_cloud(doc_id)
            if result.get("ok"):
                synced_audio = int(result.get("audio_count") or 0)
                if synced_audio <= 0:
                    synced_audio = int(result.get("uploaded_audio") or 0) + int(result.get("reused_audio") or 0)
                set_job_state(doc_id, cloudSyncStatus="complete", cloudSyncMessage=f"Auto sync {str(result.get('provider') or provider).upper()} xong ({synced_audio} audio)", cloudUsedBytes=result.get("used_bytes") or active_cloud_used_bytes(), cloudLimitBytes=result.get("limit_bytes") or active_cloud_limit_bytes())
            else:
                set_job_state(doc_id, cloudSyncStatus="error", cloudSyncMessage=str(result.get("error") or result), cloudLimitBytes=result.get("limit_bytes") or active_cloud_limit_bytes())
        except Exception as exc:
            traceback.print_exc()
            set_job_state(doc_id, cloudSyncStatus="error", cloudSyncMessage=f"Loi auto sync {provider}: {exc}", cloudLimitBytes=active_cloud_limit_bytes())
        finally:
            with CLOUD_AUTO_SYNC_LOCK:
                CLOUD_AUTO_SYNC_IN_FLIGHT.discard(doc_id)

    threading.Thread(target=runner, name=f"auto-sync-cloud-{doc_id}", daemon=True).start()


def project_manifest_for_doc(doc_id):
    store = read_project_store()
    docs = store.get("docs") if isinstance(store.get("docs"), list) else []
    doc = next((item for item in docs if item.get("id") == doc_id), None)
    manifest = doc.get("audioManifest") if isinstance(doc, dict) and isinstance(doc.get("audioManifest"), dict) else {}
    return manifest


def project_doc_for_id(doc_id):
    store = read_project_store()
    docs = store.get("docs") if isinstance(store.get("docs"), list) else []
    doc = next((item for item in docs if item.get("id") == doc_id), None)
    return dict(doc) if isinstance(doc, dict) else {}


def project_prepare_requested(doc_id):
    doc = project_doc_for_id(doc_id)
    return bool(isinstance(doc, dict) and doc.get("prepareRequested"))


def project_is_auto_prepare_candidate(doc):
    if not isinstance(doc, dict) or not doc.get("id"):
        return False
    if doc.get("sourceName") == "sample":
        return False
    if re.match(r"^Project m.u Vi.t", str(doc.get("title") or ""), re.IGNORECASE):
        return False
    return bool(str(doc.get("text") or "").strip() or doc.get("audioJobs"))


def prepared_doc_for_auto_queue(doc):
    existing_jobs = normalize_prepare_jobs(doc.get("audioJobs") if isinstance(doc, dict) else [])
    current_ready, current_total = project_mobile_audio_progress(doc)
    if existing_jobs and current_total and len(existing_jobs) >= current_total:
        jobs = existing_jobs
    else:
        jobs = build_project_prepare_jobs_for_doc(doc)
    if not jobs:
        jobs = existing_jobs
    if not jobs:
        return None, [], ""
    manifest = doc_audio_manifest(doc)
    prepared = dict(doc)
    clean_manifest = {}
    clean_items = []
    for job in jobs:
        key = str(job.get("key") or "").strip()
        if not key:
            continue
        cached_url = cached_audio_url_for_job(job, manifest)
        if cached_url:
            clean_manifest[key] = cached_url
            clean_items.append({
                "key": key,
                "text": job.get("text") or "",
                "url": cached_url,
                "voice": job.get("voice") or VIENEU_DEFAULT_VOICE,
                "voiceLabel": doc_audio_voice_label_value(doc, job.get("voice") or VIENEU_DEFAULT_VOICE),
                "modelVersion": job.get("modelVersion") or TTS_ENGINE_VERSION,
            })
    prepared["audioJobs"] = jobs
    prepared["chunkCount"] = len(jobs)
    prepared["audioManifest"] = clean_manifest
    prepared["audioItems"] = clean_items
    prepared["audioVoice"] = doc_audio_voice_value(prepared)
    prepared["audioVoiceLabel"] = doc_audio_voice_label_value(prepared, prepared["audioVoice"])
    prepared["audioEngine"] = "vieneu"
    prepared["audioSpeed"] = VIENEU_AUDIO_SPEED
    prepared["audioModelVersion"] = TTS_ENGINE_VERSION
    prepared.pop("audioLockedEngine", None)
    if len(clean_manifest) >= len(jobs):
        prepared["audioSignature"] = project_audio_signature_from_jobs(jobs)
    else:
        prepared.pop("audioSignature", None)
    return prepared, jobs, project_audio_signature_from_jobs(jobs)


def background_has_running_project():
    with BACKGROUND_LOCK:
        return any(
            isinstance(job, dict) and job.get("status") in ("queued", "running")
            for job in BACKGROUND_JOBS.values()
        )


def schedule_auto_start_next_project_prepare(previous_doc_id="", delay=0.8):
    if not AUTO_PROJECT_WATCHDOG_ENABLED:
        return None
    def runner():
        auto_start_next_project_prepare(previous_doc_id)

    timer = threading.Timer(max(0.0, float(delay or 0)), runner)
    timer.daemon = True
    timer.start()
    return timer


def start_auto_project_watchdog():
    if not AUTO_PROJECT_WATCHDOG_ENABLED:
        return

    def runner():
        time.sleep(6.0)
        while True:
            try:
                auto_start_next_project_prepare()
            except Exception:
                traceback.print_exc()
            time.sleep(AUTO_PROJECT_WATCHDOG_SECONDS)

    threading.Thread(target=runner, name="auto-project-watchdog", daemon=True).start()


def auto_start_next_project_prepare(previous_doc_id=""):
    global AUTO_PROJECT_CHAIN_IN_FLIGHT, AUTO_PROJECT_CHAIN_STARTED_TS
    if not AUTO_PROJECT_WATCHDOG_ENABLED:
        return None
    with AUTO_PROJECT_CHAIN_LOCK:
        if AUTO_PROJECT_CHAIN_IN_FLIGHT:
            age = time.time() - float(AUTO_PROJECT_CHAIN_STARTED_TS or 0.0)
            if age < 120:
                return None
            print(f"Auto project chain stale for {age:.1f}s, resetting.")
        AUTO_PROJECT_CHAIN_IN_FLIGHT = True
        AUTO_PROJECT_CHAIN_STARTED_TS = time.time()
    try:
        if background_has_running_project():
            return None
        store = read_project_store()
        docs = store.get("docs") if isinstance(store.get("docs"), list) else []
        active_id = str(store.get("activeDocId") or "").strip()
        def doc_queue_order(item):
            try:
                return int(item.get("desktopOrder"))
            except Exception:
                try:
                    return docs.index(item)
                except Exception:
                    return 1000000

        ordered_docs = sorted(
            [doc for doc in docs if isinstance(doc, dict)],
            key=doc_queue_order,
        )
        for doc in ordered_docs:
            if not project_is_auto_prepare_candidate(doc):
                continue
            prepared, jobs, signature = prepared_doc_for_auto_queue(doc)
            if not prepared or not jobs:
                continue
            ready, total = project_mobile_audio_progress(prepared)
            if total and ready >= total:
                continue
            job = start_background_project_prepare(
                prepared,
                jobs,
                audio_signature=signature,
                active_doc_id=active_id,
                force=False,
                auto_batch=True,
            )
            print(f"Auto queued next audio project: {prepared.get('title') or prepared.get('id')} ({ready}/{total})")
            return job
        return None
    except Exception:
        traceback.print_exc()
        return None
    finally:
        with AUTO_PROJECT_CHAIN_LOCK:
            AUTO_PROJECT_CHAIN_IN_FLIGHT = False
            AUTO_PROJECT_CHAIN_STARTED_TS = 0.0


def auto_project_queue_status(limit=20):
    with AUTO_PROJECT_CHAIN_LOCK:
        in_flight = bool(AUTO_PROJECT_CHAIN_IN_FLIGHT)
        in_flight_age = time.time() - float(AUTO_PROJECT_CHAIN_STARTED_TS or 0.0) if in_flight else 0.0
    with BACKGROUND_LOCK:
        running = [
            job_progress_fields(dict(job))
            for job in BACKGROUND_JOBS.values()
            if isinstance(job, dict) and job.get("status") in ("queued", "running")
        ]
    store = read_project_store()
    docs = store.get("docs") if isinstance(store.get("docs"), list) else []

    def doc_queue_order(item):
        try:
            return int(item.get("desktopOrder"))
        except Exception:
            try:
                return docs.index(item)
            except Exception:
                return 1000000

    pending = []
    checked = 0
    for doc in sorted([item for item in docs if isinstance(item, dict)], key=doc_queue_order):
        if not project_is_auto_prepare_candidate(doc):
            continue
        checked += 1
        prepared, jobs, _signature = prepared_doc_for_auto_queue(doc)
        if not prepared or not jobs:
            continue
        ready, total = project_mobile_audio_progress(prepared)
        if total and ready < total:
            pending.append({
                "docId": doc.get("id"),
                "title": doc.get("title") or "",
                "folderPath": doc.get("folderPath") or "",
                "ready": ready,
                "total": total,
                "desktopOrder": doc.get("desktopOrder"),
                "updatedAt": doc.get("updatedAt") or "",
            })
            if len(pending) >= int(limit or 20):
                break
    return {
        "ok": True,
        "autoProjectChainInFlight": in_flight,
        "autoProjectChainAgeSeconds": round(max(0.0, in_flight_age), 1),
        "runningAudioJobs": running,
        "checkedCandidates": checked,
        "pendingAudioCountShown": len(pending),
        "pendingAudio": pending,
    }


def run_background_project_prepare(doc_id, jobs, audio_signature):
    total = len(jobs)
    audio_meta = active_tts_job_metadata()
    initial_done = cached_job_count(jobs, project_manifest_for_doc(doc_id))
    set_job_state(doc_id, status="queued", done=initial_done, total=total, error="", message="Dang cho den luot xu ly", **audio_meta)
    BACKGROUND_PROJECT_SEMAPHORE.acquire()
    try:
        initial_done = cached_job_count(jobs, project_manifest_for_doc(doc_id))
        set_job_state(doc_id, status="running", done=initial_done, total=total, error="", message=f"Dang tao audio {initial_done}/{total}", **audio_meta)
        store = read_project_store()
        docs = store.get("docs") if isinstance(store.get("docs"), list) else []
        doc = next((item for item in docs if item.get("id") == doc_id), None)
        manifest = doc.get("audioManifest") if isinstance(doc, dict) and isinstance(doc.get("audioManifest"), dict) else {}
        cached_updates = {}
        for job in jobs:
            cached_url = cached_audio_url_for_job(job, manifest)
            if cached_url:
                cached_updates[job["key"]] = cached_url
        if cached_updates:
            update_project_audio(doc_id, cached_updates, None)
            manifest = {**manifest, **cached_updates}
        todo = [job for job in jobs if not cached_audio_url_for_job(job, manifest)]
        if not todo:
            update_project_audio(doc_id, {}, audio_signature)
            set_job_state(doc_id, status="complete", done=total, total=total, message="Audio da san sang", **audio_meta)
            auto_sync_project_to_cloud_background(doc_id)
            schedule_auto_start_next_project_prepare(doc_id)
            return

        completed = total - len(todo)
        set_job_state(doc_id, done=completed, total=total, message=f"Dang tao audio {completed}/{total}", **audio_meta)

        def synth_one(job):
            filename = synthesize_preferred_wav_sync(job["text"], job.get("voice") or VIENEU_DEFAULT_VOICE)
            return job["key"], audio_url(filename)

        workers = max(1, min(BACKGROUND_MAX_WORKERS, len(todo)))
        pending_manifest_updates = {}
        last_manifest_flush = time.monotonic()
        with ThreadPoolExecutor(max_workers=workers) as executor:
            futures = [executor.submit(synth_one, job) for job in todo]
            for future in as_completed(futures):
                key, url = future.result()
                completed += 1
                pending_manifest_updates[key] = url
                if len(pending_manifest_updates) >= 20 or time.monotonic() - last_manifest_flush >= 12:
                    update_project_audio(doc_id, pending_manifest_updates, None)
                    pending_manifest_updates = {}
                    last_manifest_flush = time.monotonic()
                set_job_state(
                    doc_id,
                    status="running",
                    done=completed,
                    total=total,
                    workers=workers,
                    message=f"Dang tao audio {completed}/{total}",
                    **audio_meta,
                )

        if pending_manifest_updates:
            update_project_audio(doc_id, pending_manifest_updates, None)
        update_project_audio(doc_id, {}, audio_signature)
        set_job_state(doc_id, status="complete", done=total, total=total, workers=workers, message="Audio da tao xong", **audio_meta)
        auto_sync_project_to_cloud_background(doc_id)
        schedule_auto_start_next_project_prepare(doc_id)
    except Exception as exc:
        traceback.print_exc()
        set_job_state(doc_id, status="error", error=str(exc), message=f"Loi tao audio: {exc}")
    finally:
        BACKGROUND_PROJECT_SEMAPHORE.release()


def start_background_project_prepare(doc, jobs, audio_signature="", active_doc_id="", force=False, auto_batch=False):
    if not isinstance(doc, dict) or not doc.get("id"):
        raise ValueError("Project doc id is required")
    normalized_jobs = normalize_prepare_jobs(jobs)
    rebuilt_jobs = normalize_prepare_jobs(build_project_prepare_jobs_for_doc(doc))
    _current_ready, current_total = project_mobile_audio_progress(doc)
    if rebuilt_jobs and (not normalized_jobs or (current_total and len(normalized_jobs) < current_total)):
        normalized_jobs = rebuilt_jobs
        audio_signature = project_audio_signature_from_jobs(normalized_jobs)
    if not normalized_jobs:
        raise ValueError("No audio jobs to prepare")
    doc_id = doc["id"]
    if cloud_doc_id_is_deleted(doc_id):
        with BACKGROUND_LOCK:
            BACKGROUND_JOBS.pop(doc_id, None)
        return {
            "docId": doc_id,
            "title": doc.get("title") or "",
            "status": "deleted",
            "done": 0,
            "total": len(normalized_jobs),
            "workers": 0,
            "message": "File da xoa, bo qua job audio",
            "error": "",
            **active_tts_job_metadata(),
        }
    if auto_batch:
        with BACKGROUND_LOCK:
            busy_job = next(
                (
                    item
                    for item in BACKGROUND_JOBS.values()
                    if isinstance(item, dict)
                    and item.get("docId") != doc_id
                    and item.get("status") in ("queued", "running")
                ),
                None,
            )
        if busy_job:
            set_job_state(
                doc_id,
                docId=doc_id,
                title=doc.get("title") or "",
                status="skipped",
                done=0,
                total=len(normalized_jobs),
                workers=0,
                message=f"Dang cho file khac xong: {busy_job.get('title') or busy_job.get('docId')}",
                error="",
                **active_tts_job_metadata(),
            )
            return job_snapshot(doc_id)
    existing_doc = project_doc_for_id(doc_id) or {}
    check_doc = dict(existing_doc)
    check_manifest = existing_doc.get("audioManifest") if isinstance(existing_doc.get("audioManifest"), dict) else {}
    incoming_manifest = doc.get("audioManifest") if isinstance(doc.get("audioManifest"), dict) else {}
    check_doc.update(doc)
    check_doc["audioManifest"] = {**check_manifest, **incoming_manifest}
    if not force and not auto_batch and active_doc_id != doc_id:
        set_job_state(
            doc_id,
            docId=doc_id,
            title=doc.get("title") or "",
            status="skipped",
            done=0,
            total=len(normalized_jobs),
            workers=0,
            message="Chi xu ly khi mo file hoac bam Xu ly",
            error="",
            **active_tts_job_metadata(),
        )
        return job_snapshot(doc_id)
    combined_manifest = {**check_manifest, **incoming_manifest}
    clean_manifest = {}
    clean_items = []
    for job in normalized_jobs:
        key = str(job.get("key") or "").strip()
        if not key:
            continue
        cached_url = cached_audio_url_for_job(job, combined_manifest)
        if cached_url:
            clean_manifest[key] = cached_url
            clean_items.append({
                "key": key,
                "text": job.get("text") or "",
                "url": cached_url,
                "voice": job.get("voice") or VIENEU_DEFAULT_VOICE,
                "voiceLabel": doc_audio_voice_label_value(doc, job.get("voice") or VIENEU_DEFAULT_VOICE),
                "modelVersion": job.get("modelVersion") or TTS_ENGINE_VERSION,
            })
    doc["audioManifest"] = clean_manifest
    doc["audioItems"] = clean_items
    doc["chunkCount"] = len(normalized_jobs)
    doc.pop("audioLockedEngine", None)
    doc["audioJobs"] = normalized_jobs
    _store, merged_doc = merge_project_doc(doc, active_doc_id=active_doc_id)
    doc = merged_doc
    doc_id = doc["id"]
    initial_done = cached_job_count(normalized_jobs, project_manifest_for_doc(doc_id))
    if initial_done >= len(normalized_jobs):
        update_project_audio(doc_id, {}, audio_signature)
        set_job_state(
            doc_id,
            docId=doc_id,
            title=doc.get("title") or "",
            status="complete",
            done=len(normalized_jobs),
            total=len(normalized_jobs),
            workers=0,
            message="Audio da san sang",
            error="",
            **active_tts_job_metadata(),
        )
        auto_sync_project_to_cloud_background(doc_id)
        schedule_auto_start_next_project_prepare(doc_id)
        return job_snapshot(doc_id)
    with BACKGROUND_LOCK:
        current = BACKGROUND_JOBS.get(doc_id)
        if current and current.get("status") in ("queued", "running") and not force:
            return dict(current)
        BACKGROUND_JOBS[doc_id] = {
            "docId": doc_id,
            "title": doc.get("title") or "",
            "status": "queued",
            "done": initial_done,
            "total": len(normalized_jobs),
            "workers": min(BACKGROUND_MAX_WORKERS, len(normalized_jobs)),
            "message": "Dang cho xu ly",
            "error": "",
            "startedAt": "",
            "startedTs": 0,
            "elapsedSeconds": 0,
            "etaSeconds": None,
            "percent": round(initial_done / len(normalized_jobs) * 100, 1) if normalized_jobs else 0,
            "updatedAt": time.strftime("%Y-%m-%dT%H:%M:%S"),
            **active_tts_job_metadata(),
        }
    thread = threading.Thread(
        target=run_background_project_prepare,
        args=(doc_id, normalized_jobs, audio_signature),
        name=f"prepare-audio-{doc_id}",
        daemon=False,
    )
    thread.start()
    return job_snapshot(doc_id)



TTS_METADATA_LINE_RE = re.compile(
    r"^\s*(nguồn|ngu\?n|source|model|diarization|diar model|chế độ|ch\? d\?|mode|denoise|vad|ghi chú|ghi ch\?|note|transcript)\b\s*[:\-]?",
    re.IGNORECASE,
)
TTS_SPEAKER_LINE_RE = re.compile(r"^\s*\[\s*speaker\s*\d+\s*\]\s*$", re.IGNORECASE)
TTS_TIME_PREFIX_RE = re.compile(r"^\s*\[\s*\d{1,2}:\d{2}(?::\d{2})?(?:\.\d+)?\s*\]\s*")
TTS_ACRONYM_MAP = {
    "AI": "ây ai",
    "ICT": "ai xi ti",
    "IPO": "ai pi ô",
    "CEO": "xi i ô",
    "CFO": "xi ép ô",
    "COO": "xi ô ô",
    "GDP": "gi đi pi",
    "FDI": "ép đi ai",
    "USD": "đô la Mỹ",
    "VND": "đồng",
    "MWG": "em vê giê",
    "HSG": "hát ét giê",
    "FPT": "ép pi ti",
    "MBS": "em bi ét",
    "HOSE": "hô sê",
    "HNX": "hát en ích",
    "UPCOM": "úp com",
}



def ensure_tts_pause_punctuation(text):
    value = clean_text(text or "")
    if not value:
        return ""
    if re.search(r"[.!?。！？…;:,)]$", value):
        return value
    return value + "."


def expand_acronyms_for_tts(text):
    def repl(match):
        token = match.group(0)
        mapped = TTS_ACRONYM_MAP.get(token.upper())
        if mapped:
            return mapped
        if 2 <= len(token) <= 6:
            return " ".join(token)
        return token
    return re.sub(r"\b[A-Z][A-Z0-9&]{1,7}\b", repl, text or "")


def prepare_tts_text(text):
    text = latex_to_speech(text)
    lines = []
    for raw in str(text or "").replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        line = TTS_TIME_PREFIX_RE.sub("", raw).strip()
        if not line:
            continue
        if TTS_SPEAKER_LINE_RE.match(line):
            continue
        if TTS_METADATA_LINE_RE.match(line):
            continue
        lines.append(ensure_tts_pause_punctuation(line))
    cleaned = clean_text(" ".join(lines))
    cleaned = expand_acronyms_for_tts(cleaned)
    return ensure_tts_pause_punctuation(cleaned)


def latex_to_speech(text):
    """Turn common LaTeX into readable Vietnamese before sending it to TTS."""
    value = str(text or "")
    value = re.sub(r"\\[\[(]", " ", value)
    value = re.sub(r"\\[\])]", " ", value)

    # Unwrap square brackets only when they contain clear math syntax.
    value = re.sub(
        r"\[([^\]\n]*(?:\\(?:frac|sqrt|approx|sum|prod|leq?|geq?|times|cdot)|_\{|\^)[^\]\n]*)\]",
        lambda match: f" {match.group(1)} ",
        value,
    )

    previous = None
    while previous != value:
        previous = value
        value = re.sub(
            r"\\frac\s*\{([^{}]*)\}\s*\{([^{}]*)\}",
            lambda match: f" ({match.group(1)}) chia cho ({match.group(2)}) ",
            value,
        )
        value = re.sub(
            r"\\sqrt\s*\{([^{}]*)\}",
            lambda match: f" căn bậc hai của ({match.group(1)}) ",
            value,
        )

    replacements = {
        r"\approx": " xấp xỉ ",
        r"\leq": " nhỏ hơn hoặc bằng ",
        r"\le": " nhỏ hơn hoặc bằng ",
        r"\geq": " lớn hơn hoặc bằng ",
        r"\ge": " lớn hơn hoặc bằng ",
        r"\times": " nhân ",
        r"\cdot": " nhân ",
        r"\neq": " khác ",
        r"\infty": " vô cùng ",
        r"\sum": " tổng ",
        r"\prod": " tích ",
        r"\%": " phần trăm ",
    }
    for source, target in replacements.items():
        value = value.replace(source, target)
    value = re.sub(r"_\{([^{}]+)\}", lambda match: f" chỉ số {match.group(1)} ", value)
    value = re.sub(r"\^\{([^{}]+)\}", lambda match: f" mũ {match.group(1)} ", value)
    value = re.sub(r"\^([A-Za-z0-9.,+-]+)", lambda match: f" mũ {match.group(1)} ", value)
    value = value.replace("%", " phần trăm ")
    value = re.sub(r"[{}]", " ", value)
    value = re.sub(r"\\[A-Za-z]+", " ", value)
    return clean_text(value)

def clean_text(text):
    text = (text or "").replace("\r", "")
    text = re.sub(r"[\u00a0\u200b\u200c\u200d]", " ", text)
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r" *\n *", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    text = re.sub(r"[ \t]+([,.;:!?])", r"\1", text)
    return text.strip()


def available_ocr_lang():
    names = {p.stem for p in TESSDATA_DIR.glob("*.traineddata")} if TESSDATA_DIR.exists() else set()
    if "vie" in names and "eng" in names:
        return "vie+eng"
    if "vie" in names:
        return "vie"
    return "eng"


def text_layer_from_pdf(doc):
    pages = []
    for page_no in range(len(doc)):
        page = doc[page_no]
        text_page = page.get_textpage()
        text = clean_text(text_page.get_text_range() or "")
        if text:
            pages.append(f"[Page {page_no + 1}]\n{text}")
    return "\n\n".join(pages).strip()


def render_page_for_ocr(page, dpi=220):
    zoom = max(72, min(300, int(dpi or 220))) / 72
    width, height = page.get_size()
    estimated_pixels = max(1.0, float(width) * float(height) * zoom * zoom)
    if estimated_pixels > MAX_OCR_PIXELS:
        zoom *= (MAX_OCR_PIXELS / estimated_pixels) ** 0.5
    bitmap = page.render(scale=zoom)
    img = bitmap.to_pil().convert("RGB")
    img = ImageOps.grayscale(img)
    img = ImageOps.autocontrast(img)
    return img


def ocr_pdf(doc, max_pages=0, dpi=220):
    if not TESSERACT_EXE.exists():
        raise RuntimeError("Missing Tesseract at C:\\Program Files\\Tesseract-OCR\\tesseract.exe")
    if not TESSDATA_DIR.exists():
        raise RuntimeError("Missing tessdata folder next to reader_server.py")

    page_count = len(doc)
    requested = MAX_OCR_PAGES if not max_pages else max(1, int(max_pages))
    limit = min(page_count, requested, MAX_OCR_PAGES)
    lang = available_ocr_lang()
    config = "--oem 1 --psm 6"
    pages = []

    for page_index in range(limit):
        image = render_page_for_ocr(doc[page_index], dpi=dpi)
        text = pytesseract.image_to_string(image, lang=lang, config=config)
        text = clean_text(text)
        if text:
            pages.append(f"[Page {page_index + 1}]\n{text}")

    return "\n\n".join(pages).strip(), limit, lang


def extract_pdf_bytes(pdf_bytes, max_pages=0, force_ocr=False):
    if not pdf_bytes:
        raise ValueError("PDF data is empty")
    if len(pdf_bytes) > MAX_PDF_INPUT_BYTES:
        raise ValueError(f"PDF is too large (max {MAX_PDF_INPUT_BYTES // 1024 // 1024} MB)")
    doc = pdfium.PdfDocument(pdf_bytes)
    try:
        page_count = len(doc)
        if not force_ocr:
            text = text_layer_from_pdf(doc)
            if len(text) >= 120:
                return {
                    "text": text,
                    "method": "text_layer",
                    "pages": page_count,
                    "processed_pages": page_count,
                    "ocr_lang": "",
                    "char_count": len(text),
                }

        text, processed, lang = ocr_pdf(doc, max_pages=max_pages)
        return {
            "text": text,
            "method": "ocr",
            "pages": page_count,
            "processed_pages": processed,
            "ocr_lang": lang,
            "char_count": len(text),
        }
    finally:
        doc.close()


W_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
M_NS = "http://schemas.openxmlformats.org/officeDocument/2006/math"
W_TEXT = f"{{{W_NS}}}t"
W_TAB = f"{{{W_NS}}}tab"
W_BR = f"{{{W_NS}}}br"
W_CR = f"{{{W_NS}}}cr"
M_OMATH = f"{{{M_NS}}}oMath"
M_OMATH_PARA = f"{{{M_NS}}}oMathPara"


def xml_local_name(tag):
    return str(tag or "").rsplit("}", 1)[-1]


def omml_child(node, name):
    return next((child for child in list(node) if xml_local_name(child.tag) == name), None)


def omml_attr_value(node, default=""):
    if node is None:
        return default
    return next((str(value) for value in node.attrib.values() if value is not None), default)


def omml_text_value(text):
    value = str(text or "")
    replacements = {
        "−": "-", "×": r"\times ", "·": r"\cdot ", "÷": r"\div ",
        "≈": r"\approx ", "≤": r"\leq ", "≥": r"\geq ", "≠": r"\neq ",
        "∞": r"\infty ", "∑": r"\sum ", "∏": r"\prod ", "√": r"\sqrt{}",
        "%": r"\%",
    }
    for source, target in replacements.items():
        value = value.replace(source, target)
    return value


def omml_to_latex(node):
    if node is None:
        return ""
    tag = xml_local_name(node.tag)
    ignored = {"fPr", "radPr", "sSubPr", "sSupPr", "sSubSupPr", "dPr", "naryPr", "funcPr", "accPr", "barPr", "mPr", "mrPr", "ctrlPr", "rPr"}
    if tag in ignored:
        return ""
    if tag == "t":
        return omml_text_value(node.text)
    if tag == "f":
        return rf"\frac{{{omml_to_latex(omml_child(node, 'num'))}}}{{{omml_to_latex(omml_child(node, 'den'))}}}"
    if tag == "rad":
        degree = omml_to_latex(omml_child(node, "deg")).strip()
        body = omml_to_latex(omml_child(node, "e"))
        return rf"\sqrt[{degree}]{{{body}}}" if degree else rf"\sqrt{{{body}}}"
    if tag == "sSub":
        return rf"{omml_to_latex(omml_child(node, 'e'))}_{{{omml_to_latex(omml_child(node, 'sub'))}}}"
    if tag == "sSup":
        return rf"{omml_to_latex(omml_child(node, 'e'))}^{{{omml_to_latex(omml_child(node, 'sup'))}}}"
    if tag == "sSubSup":
        return rf"{omml_to_latex(omml_child(node, 'e'))}_{{{omml_to_latex(omml_child(node, 'sub'))}}}^{{{omml_to_latex(omml_child(node, 'sup'))}}}"
    if tag == "d":
        props = omml_child(node, "dPr")
        begin = omml_attr_value(omml_child(props, "begChr"), "(")
        end = omml_attr_value(omml_child(props, "endChr"), ")")
        body = "".join(omml_to_latex(child) for child in list(node) if xml_local_name(child.tag) == "e")
        return rf"\left{begin}{body}\right{end}"
    if tag == "nary":
        props = omml_child(node, "naryPr")
        operator = omml_attr_value(omml_child(props, "chr"), "∑")
        operator = omml_text_value(operator).strip()
        sub = omml_to_latex(omml_child(node, "sub")).strip()
        sup = omml_to_latex(omml_child(node, "sup")).strip()
        body = omml_to_latex(omml_child(node, "e"))
        limits = (rf"_{{{sub}}}" if sub else "") + (rf"^{{{sup}}}" if sup else "")
        return f"{operator}{limits} {body}"
    if tag == "func":
        return f"{omml_to_latex(omml_child(node, 'fName'))} {omml_to_latex(omml_child(node, 'e'))}"
    if tag == "acc":
        props = omml_child(node, "accPr")
        accent = omml_attr_value(omml_child(props, "chr"), "ˆ")
        commands = {"ˆ": "hat", "^": "hat", "¯": "bar", "→": "vec", "˜": "tilde", "~": "tilde"}
        command = commands.get(accent, "hat")
        return rf"\{command}{{{omml_to_latex(omml_child(node, 'e'))}}}"
    if tag == "bar":
        return rf"\overline{{{omml_to_latex(omml_child(node, 'e'))}}}"
    if tag in ("limLow", "limUpp"):
        base = omml_to_latex(omml_child(node, "e"))
        limit = omml_to_latex(omml_child(node, "lim"))
        marker = "_" if tag == "limLow" else "^"
        return rf"{base}{marker}{{{limit}}}"
    if tag == "m":
        rows = []
        for row in [child for child in list(node) if xml_local_name(child.tag) == "mr"]:
            cells = [omml_to_latex(child) for child in list(row) if xml_local_name(child.tag) == "e"]
            rows.append(" & ".join(cells))
        return r"\begin{matrix}" + r" \\ ".join(rows) + r"\end{matrix}"
    return "".join(omml_to_latex(child) for child in list(node))


def docx_node_parts(node):
    parts = []
    for child in list(node):
        if child.tag == W_TEXT and child.text:
            parts.append(child.text)
        elif child.tag in (M_OMATH, M_OMATH_PARA):
            latex = re.sub(r"\s+", " ", omml_to_latex(child)).strip()
            if latex:
                parts.append(rf"\({latex}\)")
        elif child.tag == W_TAB:
            parts.append("\t")
        elif child.tag in (W_BR, W_CR):
            parts.append("\n")
        else:
            parts.extend(docx_node_parts(child))
    return parts


def docx_xml_to_paragraphs(xml_bytes):
    root = ET.fromstring(xml_bytes)
    paragraphs = []
    for paragraph in root.iter(f"{{{W_NS}}}p"):
        parts = docx_node_parts(paragraph)
        text = clean_text("".join(parts))
        if text:
            paragraphs.append(text)
    math_count = sum(1 for node in root.iter(M_OMATH))
    return paragraphs, math_count


def extract_docx_bytes(docx_bytes):
    if not docx_bytes:
        raise ValueError("DOCX data is empty")
    if len(docx_bytes) > MAX_DOCX_INPUT_BYTES:
        raise ValueError(f"DOCX is too large (max {MAX_DOCX_INPUT_BYTES // 1024 // 1024} MB)")

    with zipfile.ZipFile(io.BytesIO(docx_bytes)) as zf:
        if len(zf.infolist()) > 10000:
            raise ValueError("DOCX has too many archive entries")
        names = set(zf.namelist())
        if "word/document.xml" not in names:
            raise ValueError("File is not a valid DOCX document")

        ordered = ["word/document.xml"]
        ordered.extend(sorted(
            name for name in names
            if name.startswith("word/header") and name.endswith(".xml")
        ))
        ordered.extend(sorted(
            name for name in names
            if name.startswith("word/footer") and name.endswith(".xml")
        ))
        ordered.extend(name for name in ["word/footnotes.xml", "word/endnotes.xml", "word/comments.xml"] if name in names)
        uncompressed_xml_bytes = sum(zf.getinfo(name).file_size for name in ordered)
        if uncompressed_xml_bytes > MAX_DOCX_XML_BYTES:
            raise ValueError(f"DOCX XML is too large (max {MAX_DOCX_XML_BYTES // 1024 // 1024} MB)")

        paragraphs = []
        math_count = 0
        for name in ordered:
            try:
                part_paragraphs, part_math_count = docx_xml_to_paragraphs(zf.read(name))
                paragraphs.extend(part_paragraphs)
                math_count += part_math_count
            except ET.ParseError:
                continue

    text = clean_text("\n\n".join(paragraphs))
    return {
        "text": text,
        "method": "docx_xml",
        "parts": len(ordered),
        "math_count": math_count,
        "char_count": len(text),
    }


def clamp_rate(rate):
    try:
        value = float(rate)
    except Exception:
        value = 1.0
    value = max(0.5, min(2.0, value))
    return f"{round((value - 1.0) * 100):+d}%"


def cache_filename(text, voice, rate_arg):
    key = json.dumps({"text": text, "voice": voice, "rate": rate_arg}, ensure_ascii=False, sort_keys=True)
    digest = hashlib.sha256(key.encode("utf-8")).hexdigest()[:28]
    safe_voice = re.sub(r"[^A-Za-z0-9_-]+", "_", voice)[:70]
    return f"{safe_voice}_{digest}.mp3"


async def edge_voice_list():
    voices = [
        {"name": "Trúc Ly", "display": "Trúc Ly [VieNeu v3]"},
        {"name": "Ngọc Linh", "display": "Ngọc Linh [VieNeu v3]"},
        {"name": "Ngọc Lan", "display": "Ngọc Lan [VieNeu v3]"},
        {"name": "Mỹ Duyên", "display": "Mỹ Duyên [VieNeu v3]"},
        {"name": "Ly", "display": "Ly [VieNeu cũ]"},
    ]
    return [{
        "name": item.get("name") or "",
        "display": item.get("display") or item.get("name") or "",
        "locale": "vi-VN",
        "gender": "VieNeu",
    } for item in voices]



def vieneu_cache_filename(text, voice):
    normalized_voice = voice or VIENEU_DEFAULT_VOICE
    key = json.dumps({
        "engine": "vieneu",
        "model_version": VIENEU_MODEL_VERSION,
        "speed": VIENEU_AUDIO_SPEED,
        "voice": normalized_voice,
        "text": text,
    }, ensure_ascii=False, sort_keys=True)
    digest = hashlib.sha256(key.encode("utf-8")).hexdigest()[:28]
    safe_voice = re.sub(r"[^A-Za-z0-9_-]+", "_", normalized_voice)[:32]
    safe_model = re.sub(r"[^A-Za-z0-9_-]+", "_", VIENEU_MODEL_VERSION)[:36]
    return f"vieneu_{safe_model}_{VIENEU_AUDIO_SPEED}_{safe_voice}_{digest}.wav"


def legacy_vieneu_cache_filename(text, voice, model_version=None, speed=None):
    normalized_voice = voice or VIENEU_DEFAULT_VOICE
    model_version = model_version or VIENEU_MODEL_VERSION
    speed = speed or VIENEU_AUDIO_SPEED
    key = json.dumps({
        "engine": "vieneu",
        "model_version": model_version,
        "speed": speed,
        "voice": normalized_voice,
        "text": text,
    }, ensure_ascii=False, sort_keys=True)
    digest = hashlib.sha256(key.encode("utf-8")).hexdigest()[:28]
    safe_voice = re.sub(r"[^A-Za-z0-9_-]+", "_", normalized_voice)[:32]
    safe_model = re.sub(r"[^A-Za-z0-9_-]+", "_", model_version)[:36]
    return f"vieneu_{safe_model}_{speed}_{safe_voice}_{digest}.wav"


def normalize_tts_voice(value, default=None):
    default = default or VIENEU_DEFAULT_VOICE
    value = str(value or "").strip()
    if not value:
        return default
    lowered = value.lower()
    if lowered.startswith("vieneu:"):
        value = value.split(":", 1)[1].strip()
    return value or default


def find_cached_legacy_vieneu_audio(text, voice):
    normalized_voice = normalize_tts_voice(voice, default="Ly")
    if normalized_voice.lower() != "ly":
        return None
    prepared_text = clean_text(text)
    if not prepared_text:
        return None
    if len(prepared_text) > 2600:
        prepared_text = prepared_text[:2600]
    candidates = [vieneu_cache_filename(prepared_text, normalized_voice)]
    model_fallbacks = [VIENEU_LEGACY_MODEL_VERSION] if ALLOW_LEGACY_VIENEU_CACHE else []
    speed_fallbacks = [VIENEU_AUDIO_SPEED, "1.0x"]
    for model_version in model_fallbacks:
        for speed in speed_fallbacks:
            if model_version == VIENEU_MODEL_VERSION and speed == VIENEU_AUDIO_SPEED:
                continue
            candidates.append(legacy_vieneu_cache_filename(prepared_text, normalized_voice, model_version, speed))
    for filename in dict.fromkeys(candidates):
        if resolve_audio_path(filename):
            return filename
    return None


def worker_json(path, payload=None, timeout=60, port=None):
    target_port = port or VIENEU_PORT
    url = f"http://{HOST}:{target_port}{path}"
    headers = {"X-Local-Reader-Token": VIENEU_WORKER_TOKEN}
    if payload is None:
        request = urllib.request.Request(url, headers=headers, method="GET")
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8"))
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    headers["Content-Type"] = "application/json"
    request = urllib.request.Request(
        url,
        data=data,
        headers=headers,
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


def worker_health_is_compatible(health):
    if not isinstance(health, dict) or not health.get("ok"):
        return False
    if health.get("protocol") != WORKER_PROTOCOL_ID or health.get("engine") != "vieneu-tts":
        return False
    model = health.get("model") if isinstance(health.get("model"), dict) else {}
    kwargs = model.get("kwargs") if isinstance(model.get("kwargs"), dict) else {}
    mode = str(kwargs.get("mode") or model.get("label") or "").lower()
    return "v3turbo" in mode


def stop_process(process):
    if process is None or process.poll() is not None:
        return
    try:
        if os.name == "nt":
            subprocess.run(
                ["taskkill", "/PID", str(process.pid), "/T", "/F"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=10,
            )
        else:
            process.terminate()
    except Exception:
        pass


def stop_vieneu_workers():
    SERVER_SHUTDOWN_EVENT.set()
    with VIENEU_LOCK:
        processes = list(VIENEU_PROCESSES.values())
        VIENEU_PROCESSES.clear()
    with VIENEU_READY_LOCK:
        VIENEU_READY_PORTS.clear()
    for process in processes:
        stop_process(process)


def ensure_vieneu_worker(port=None, timeout=180):
    if not VIENEU_ENABLED:
        raise RuntimeError("VieNeu TTS is disabled")
    if SERVER_SHUTDOWN_EVENT.is_set():
        raise RuntimeError("Local Reader is shutting down")
    port = port or VIENEU_PORT
    try:
        health = worker_json("/health", timeout=2, port=port)
        if worker_health_is_compatible(health):
            return health
    except Exception:
        pass

    with VIENEU_LOCK:
        if SERVER_SHUTDOWN_EVENT.is_set():
            raise RuntimeError("Local Reader is shutting down")
        process = VIENEU_PROCESSES.get(port)
        if process is None or process.poll() is not None:
            if not VIENEU_PYTHON.exists():
                raise RuntimeError(f"VieNeu venv not found: {VIENEU_PYTHON}")
            if not VIENEU_WORKER.exists():
                raise RuntimeError(f"VieNeu worker not found: {VIENEU_WORKER}")
            env = os.environ.copy()
            env["PYTHONIOENCODING"] = "utf-8"
            env["HF_HUB_DISABLE_SYMLINKS_WARNING"] = "1"
            if VIENEU_SITE_PACKAGES:
                old_pythonpath = env.get("PYTHONPATH", "")
                env["PYTHONPATH"] = str(VIENEU_SITE_PACKAGES) + (os.pathsep + old_pythonpath if old_pythonpath else "")
            env["VIENEU_WORKER_PORT"] = str(port)
            env["LOCAL_READER_AUDIO_DIR"] = str(AUDIO_DIR)
            RUNTIME_ROOT.mkdir(parents=True, exist_ok=True)
            worker_log = open(RUNTIME_ROOT / f"vieneu_worker_{port}.log", "a", encoding="utf-8", buffering=1)
            worker_log.write(f"\n--- start VieNeu worker {port} ---\n")
            VIENEU_PROCESSES[port] = subprocess.Popen(
                [str(VIENEU_PYTHON), "-B", str(VIENEU_WORKER), str(port)],
                cwd=str(ROOT),
                env=env,
                stdout=worker_log,
                stderr=subprocess.STDOUT,
                creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
            )

    deadline = time.time() + timeout
    last_error = None
    while time.time() < deadline:
        if SERVER_SHUTDOWN_EVENT.is_set():
            raise RuntimeError("Local Reader is shutting down")
        process = VIENEU_PROCESSES.get(port)
        if process is not None and process.poll() is not None:
            raise RuntimeError(f"VieNeu worker {port} exited with code {process.returncode}")
        try:
            health = worker_json("/health", timeout=3, port=port)
            if worker_health_is_compatible(health):
                return health
            last_error = health.get("error") or f"Incompatible worker: {health}"
        except Exception as exc:
            last_error = exc
        time.sleep(1.0)
    raise RuntimeError(f"VieNeu worker {port} did not become ready: {last_error}")


def ensure_vieneu_workers(timeout=180):
    ready = []
    first_error = None
    for port in VIENEU_PORTS:
        try:
            ensure_vieneu_worker(port, timeout=timeout)
            ready.append(port)
        except Exception as exc:
            print(f"VieNeu worker {port} unavailable: {exc}", flush=True)
            if first_error is None:
                first_error = exc
    if not ready:
        raise RuntimeError(first_error or "No VieNeu worker is available")
    with VIENEU_READY_LOCK:
        VIENEU_READY_PORTS[:] = ready
    return ready



def warm_vieneu_workers_background():
    def runner():
        if SERVER_SHUTDOWN_EVENT.is_set():
            return
        try:
            ensure_vieneu_workers(timeout=240)
        except Exception as exc:
            print(f"VieNeu warmup failed: {exc}", flush=True)
    threading.Thread(target=runner, name="warm-vieneu-workers", daemon=True).start()


def acquire_worker_slot(ports):
    global VIENEU_ASSIGN_CURSOR
    ports = list(ports or [VIENEU_PORT])
    with VIENEU_ASSIGN_LOCK:
        start = VIENEU_ASSIGN_CURSOR % len(ports)
        VIENEU_ASSIGN_CURSOR += 1
    ordered = ports[start:] + ports[:start]
    for port in ordered:
        slot = VIENEU_WORKER_SLOTS.setdefault(port, threading.Lock())
        if slot.acquire(blocking=False):
            return port, slot
    port = ordered[0]
    slot = VIENEU_WORKER_SLOTS.setdefault(port, threading.Lock())
    slot.acquire()
    return port, slot


def clean_vieneu_wav_file(path):
    if not AUDIO_POSTPROCESS_ENABLED:
        return {"ok": True, "enabled": False}
    path = Path(path)
    try:
        with wave.open(str(path), "rb") as reader:
            params = reader.getparams()
            if params.sampwidth != 2 or params.nchannels <= 0 or params.nframes <= 0:
                return {"ok": True, "enabled": True, "skipped": "unsupported_wav"}
            raw = reader.readframes(params.nframes)
        samples = array("h")
        samples.frombytes(raw)
        if sys.byteorder != "little":
            samples.byteswap()
        if not samples:
            return {"ok": True, "enabled": True, "skipped": "empty"}

        channels = int(params.nchannels)
        counts = [0] * channels
        sums = [0.0] * channels
        for index, value in enumerate(samples):
            channel = index % channels
            sums[channel] += float(value)
            counts[channel] += 1
        offsets = [(sums[index] / counts[index]) if counts[index] else 0.0 for index in range(channels)]

        threshold = 0.78
        knee = 1.0 - threshold
        cleaned = array("h")
        pre_peak = 0
        post_peak = 0.0
        post_square = 0.0
        for index, value in enumerate(samples):
            pre_peak = max(pre_peak, abs(int(value)))
            x = (float(value) - offsets[index % channels]) / 32768.0
            x = max(-1.0, min(1.0, x))
            sign = -1.0 if x < 0 else 1.0
            ax = abs(x)
            if ax > threshold:
                ax = threshold + knee * (1.0 - pow(2.718281828, -(ax - threshold) / max(0.001, knee)))
            y = sign * min(1.0, ax)
            post_peak = max(post_peak, abs(y))
            post_square += y * y
            cleaned.append(max(-32768, min(32767, int(round(y * 32767.0)))))

        rms = (post_square / max(1, len(cleaned))) ** 0.5
        gain = 1.0
        if post_peak > AUDIO_POSTPROCESS_TARGET_PEAK:
            gain = min(gain, AUDIO_POSTPROCESS_TARGET_PEAK / post_peak)
        if rms > AUDIO_POSTPROCESS_TARGET_RMS:
            gain = min(gain, AUDIO_POSTPROCESS_TARGET_RMS / rms)
        if gain < 0.999:
            for index, value in enumerate(cleaned):
                cleaned[index] = max(-32768, min(32767, int(round(value * gain))))

        if sys.byteorder != "little":
            cleaned.byteswap()
        tmp = path.with_suffix(".clean.tmp.wav")
        with wave.open(str(tmp), "wb") as writer:
            writer.setparams(params)
            writer.writeframes(cleaned.tobytes())
        tmp.replace(path)
        return {
            "ok": True,
            "enabled": True,
            "version": AUDIO_POSTPROCESS_VERSION,
            "pre_peak": pre_peak,
            "gain": round(gain, 4),
            "target_peak": AUDIO_POSTPROCESS_TARGET_PEAK,
            "target_rms": AUDIO_POSTPROCESS_TARGET_RMS,
        }
    except Exception as exc:
        print(f"Audio clean skipped for {path.name}: {exc}", flush=True)
        return {"ok": False, "enabled": True, "error": str(exc)}


def synthesize_vieneu_wav_sync(text, voice=None):
    if not VIENEU_ENABLED:
        raise RuntimeError("VieNeu TTS is disabled")
    # Apply the same math/symbol pronunciation rules for direct API calls as
    # for queued document chunks. This prevents VieNeu from reading raw LaTeX
    # commands such as "backslash frac" when a segment is synthesized alone.
    text = prepare_tts_text(text)
    if not text:
        raise ValueError("Text is empty")
    if len(text) > 2600:
        text = text[:2600]
    voice = voice or VIENEU_DEFAULT_VOICE
    filename = vieneu_cache_filename(text, voice)
    out_path = AUDIO_DIR / filename
    if resolve_audio_path(filename):
        return filename
    ready_ports = ensure_vieneu_workers()
    device = re.sub(r"[^A-Za-z0-9_-]+", "_", os.environ.get("COMPUTERNAME") or "device")[:24]
    tmp_path = out_path.with_name(
        f"{out_path.stem}.{device}.{os.getpid()}.{threading.get_ident()}.{time.time_ns()}.tmp.wav"
    )
    port, slot = acquire_worker_slot(ready_ports)
    try:
        result = worker_json("/tts", {
            "text": text,
            "voice_name": voice,
            "output": str(tmp_path),
            "temperature": 0.8,
            "top_k": 25,
            "top_p": 0.95,
            "repetition_penalty": 1.2,
            "max_chars": 256,
        }, timeout=600, port=port)
    finally:
        slot.release()
    if not result.get("ok"):
        raise RuntimeError(result.get("error") or "VieNeu TTS failed")
    if not tmp_path.exists() or tmp_path.stat().st_size <= 0:
        raise RuntimeError("VieNeu worker did not create audio")
    clean_vieneu_wav_file(tmp_path)
    tmp_path.replace(out_path)
    return filename


def synthesize_preferred_wav_sync(text, voice=None):
    raw_voice = str(voice or "").strip()
    requested_voice = normalize_tts_voice(voice or VIENEU_DEFAULT_VOICE)
    if raw_voice.lower().startswith("vieneu:"):
        return synthesize_vieneu_wav_sync(text, requested_voice)
    return synthesize_vieneu_wav_sync(text, requested_voice)


async def synthesize_mp3(text, voice, rate):
    return await asyncio.to_thread(synthesize_preferred_wav_sync, text, voice)


class LocalReaderHTTPServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True
    request_queue_size = 64


class Handler(BaseHTTPRequestHandler):
    server_version = "LocalReader/3.2"

    def setup(self):
        super().setup()
        try:
            self.connection.settimeout(120)
        except Exception:
            pass

    def log_message(self, fmt, *args):
        message = fmt % args
        if " 200 " in message and any(path in message for path in ("/api/project_jobs", "/api/cloud/status", "/api/health")):
            return
        print("[%s] %s" % (self.log_date_time_string(), message))

    def trusted_request(self):
        try:
            if not ipaddress.ip_address(str(self.client_address[0]).split("%", 1)[0]).is_loopback:
                return False
        except Exception:
            return False
        host = str(self.headers.get("Host") or "").strip().lower()
        allowed_hosts = {f"127.0.0.1:{PORT}", f"localhost:{PORT}", f"[::1]:{PORT}", "127.0.0.1", "localhost", "[::1]"}
        if host not in allowed_hosts:
            return False
        origin = str(self.headers.get("Origin") or "").strip().lower()
        allowed_origins = {f"http://127.0.0.1:{PORT}", f"http://localhost:{PORT}", f"http://[::1]:{PORT}"}
        return not origin or origin in allowed_origins

    def reject_untrusted_request(self):
        if self.trusted_request():
            return False
        status, body, ctype = make_json({"ok": False, "error": "Local request origin is not allowed"}, 403)
        self.send_payload(status, body, ctype)
        return True

    def end_headers(self):
        origin = str(self.headers.get("Origin") or "").strip().lower()
        if origin in {f"http://127.0.0.1:{PORT}", f"http://localhost:{PORT}", f"http://[::1]:{PORT}"}:
            self.send_header("Access-Control-Allow-Origin", origin)
            self.send_header("Vary", "Origin")
            self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
            self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        super().end_headers()

    def send_payload(self, status, body, content_type):
        # The project manifest is large on multi-device libraries.  Browsers
        # advertise gzip and transparently decode it, which avoids long UI
        # stalls while keeping the local JSON API backward compatible for
        # clients that do not advertise compression.
        accept_encoding = str(self.headers.get("Accept-Encoding") or "").lower()
        if (
            body
            and "gzip" in accept_encoding
            and (str(content_type).startswith("application/json") or str(content_type).startswith("text/"))
            and len(body) >= 1024
        ):
            compressed = gzip.compress(body, compresslevel=6)
            if len(compressed) < len(body):
                body = compressed
                self.send_response(status)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Encoding", "gzip")
                self.send_header("Vary", "Accept-Encoding")
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                try:
                    self.wfile.write(body)
                except (BrokenPipeError, ConnectionAbortedError, ConnectionResetError):
                    pass
                return
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionAbortedError, ConnectionResetError):
            pass

    def do_OPTIONS(self):
        if self.reject_untrusted_request():
            return
        self.send_response(204)
        self.end_headers()

    def do_GET(self):
        if self.reject_untrusted_request():
            return
        parsed = urlparse(self.path)

        if parsed.path in ("/phone", "/phone.html"):
            phone_path = ROOT / "phone.html"
            if phone_path.exists():
                self.send_payload(200, phone_path.read_bytes(), "text/html; charset=utf-8")
                return
            status, body, ctype = make_json({"ok": False, "error": "phone.html not found"}, 404)
            self.send_payload(status, body, ctype)
            return

        if parsed.path == "/phone_manifest.json":
            manifest_path = ROOT / "phone_manifest.json"
            if manifest_path.exists():
                self.send_payload(200, manifest_path.read_bytes(), "application/manifest+json; charset=utf-8")
                return
            status, body, ctype = make_json({"ok": False, "error": "phone_manifest.json not found"}, 404)
            self.send_payload(status, body, ctype)
            return

        if parsed.path in ("/", "/index.html", "/reader_webspeech.html"):
            index_path = ROOT / "index.html"
            if index_path.exists():
                html = index_path.read_text(encoding="utf-8")
                # Some locked-down Edge profiles fetch local external scripts
                # but refuse to execute them. Inline the trusted vendored math
                # runtime while serving; no document content leaves the PC.
                for asset_name in ("katex.min.js", "auto-render.min.js"):
                    asset_path = ROOT / "vendor" / "katex" / asset_name
                    marker = f'<script src="vendor/katex/{asset_name}"></script>'
                    if asset_path.exists() and marker in html:
                        script = asset_path.read_text(encoding="utf-8")
                        # Run the UMD bundle inside a private CommonJS shim, then
                        # deliberately publish its export to the browser window.
                        # This is stable in WebView/Edge hosts that inject their own
                        # module globals or isolate inline-script globals.
                        global_name = (
                            "katex" if asset_name == "katex.min.js" else "renderMathInElement"
                        )
                        browser_script = (
                            "(function(){try{\n"
                            "var localModule={exports:{}};\n"
                            "(function(module,exports,define,require){\n"
                            + script
                            + "\n}).call(window,localModule,localModule.exports,undefined,"
                            + "function(name){if(name==='katex')return window.katex;throw new Error('Unknown local module: '+name);});\n"
                            + f"window.{global_name}=localModule.exports&&localModule.exports.default"
                            + f"?localModule.exports.default:localModule.exports;\n"
                            + "}catch(error){window.__lrMathBootErrors=(window.__lrMathBootErrors||[]).concat(String(error&&error.stack||error));}})();"
                        )
                        html = html.replace(
                            marker,
                            f'<script data-local-math="{asset_name}">{browser_script}</script>',
                        )
                self.send_payload(200, html.encode("utf-8"), "text/html; charset=utf-8")
                return
            status, body, ctype = make_json({"ok": False, "error": "index.html not found"}, 404)
            self.send_payload(status, body, ctype)
            return

        if parsed.path.startswith("/vendor/katex/"):
            vendor_root = (ROOT / "vendor" / "katex").resolve()
            asset_path = (ROOT / parsed.path.lstrip("/")).resolve()
            try:
                asset_path.relative_to(vendor_root)
            except ValueError:
                asset_path = vendor_root / "__invalid__"
            if asset_path.is_file():
                ctype = mimetypes.guess_type(str(asset_path))[0] or "application/octet-stream"
                self.send_payload(200, asset_path.read_bytes(), ctype)
                return
            status, body, ctype = make_json({"ok": False, "error": "Math asset not found"}, 404)
            self.send_payload(status, body, ctype)
            return

        if parsed.path == "/api/health":
            langs = sorted(p.stem for p in TESSDATA_DIR.glob("*.traineddata")) if TESSDATA_DIR.exists() else []
            status, body, ctype = make_json({
                "ok": True,
                "app_build": APP_BUILD_ID,
                "engine": "vieneu-tts",
                "bind_host": BIND_HOST,
                "port": PORT,
                "cloud_enabled": CLOUD_ENABLED,
                "server_python": sys.executable,
                "server_prefix": sys.prefix,
                "tts_engine": active_tts_engine_label(),
                "audio_model_version": TTS_ENGINE_VERSION,
                "nvidia_gpu": NVIDIA_GPU_AVAILABLE,
                "vieneu": VIENEU_PYTHON.exists() and VIENEU_WORKER.exists(),
                "vieneu_enabled": VIENEU_ENABLED,
                "vieneu_python": str(VIENEU_PYTHON),
                "vieneu_port": VIENEU_PORT,
                "vieneu_ports": VIENEU_PORTS,
                "vieneu_parallel_workers": len(VIENEU_PORTS),
                "background_workers": BACKGROUND_MAX_WORKERS,
                "auto_project_watchdog": AUTO_PROJECT_WATCHDOG_ENABLED,
                "auto_project_watchdog_seconds": AUTO_PROJECT_WATCHDOG_SECONDS,
                "vieneu_model_version": VIENEU_MODEL_VERSION,
                "vieneu_model_format": os.environ.get("LOCAL_READER_VIENEU_MODEL_FORMAT") or "full-fp32",
                "vieneu_backbone_repo": os.environ.get("LOCAL_READER_VIENEU_BACKBONE_REPO") or "pnnbao-ump/VieNeu-TTS-0.3B",
                "vieneu_audio_speed": VIENEU_AUDIO_SPEED,
                "audio_postprocess": AUDIO_POSTPROCESS_ENABLED,
                "audio_postprocess_version": AUDIO_POSTPROCESS_VERSION,
                "audio_postprocess_target_peak": AUDIO_POSTPROCESS_TARGET_PEAK,
                "audio_postprocess_target_rms": AUDIO_POSTPROCESS_TARGET_RMS,
                "docx": True,
                "tesseract": TESSERACT_EXE.exists(),
                "tesseract_path": str(TESSERACT_EXE),
                "tessdata_dir": str(TESSDATA_DIR),
                "tessdata": langs,
                "audio_cache": str(AUDIO_DIR),
                "legacy_shared_audio_cache": str(SHARED_AUDIO_DIR),
                "app_root": str(ROOT),
                "runtime_root": str(RUNTIME_ROOT),
                "device_id": current_device_id(),
                "device_progress_dir": str(DEVICE_PROGRESS_DIR),
                "project_store_mode": "per-device-merge-v3-delta",
                "project_device_store": str(project_device_store_path()),
            })
            self.send_payload(status, body, ctype)
            return

        if parsed.path == "/api/voices":
            try:
                voices = asyncio.run(edge_voice_list())
                status, body, ctype = make_json({"ok": True, "voices": voices})
            except Exception as exc:
                status, body, ctype = make_json({"ok": False, "error": str(exc)}, 500)
            self.send_payload(status, body, ctype)
            return

        if parsed.path == "/api/projects_boot":
            query = parse_qs(parsed.query or "")
            requested_doc_id = str((query.get("activeDocId") or [""])[0] or "").strip()
            status, body, ctype = make_json({"ok": True, **project_boot_store(requested_doc_id)})
            self.send_payload(status, body, ctype)
            return

        if parsed.path == "/api/project":
            query = parse_qs(parsed.query or "")
            doc_id = str((query.get("id") or [""])[0] or "").strip()
            store = read_project_store()
            doc = next(
                (
                    item
                    for item in (store.get("docs") or [])
                    if isinstance(item, dict) and str(item.get("id") or "") == doc_id
                ),
                None,
            )
            if doc is None:
                status, body, ctype = make_json({"ok": False, "error": "Project not found"}, 404)
            else:
                status, body, ctype = make_json({"ok": True, "doc": doc, "savedAt": store.get("savedAt") or ""})
            self.send_payload(status, body, ctype)
            return

        if parsed.path == "/api/projects":
            store = read_project_store()
            status, body, ctype = make_json({"ok": True, **store})
            self.send_payload(status, body, ctype)
            return

        if parsed.path == "/api/progress":
            status, body, ctype = make_json({
                "ok": True,
                "deviceId": current_device_id(),
                "docs": read_all_device_progress(),
            })
            self.send_payload(status, body, ctype)
            return

        if parsed.path == "/api/project_jobs":
            status, body, ctype = make_json({"ok": True, "jobs": job_snapshot()})
            self.send_payload(status, body, ctype)
            return

        if parsed.path == "/api/project_auto_status":
            result = auto_project_queue_status()
            status, body, ctype = make_json(result)
            self.send_payload(status, body, ctype)
            return

        if parsed.path == "/api/cloud/status":
                result = cloud_status_payload()
                status, body, ctype = make_json(result, 200 if result.get("ok") else 400)
                self.send_payload(status, body, ctype)
                return

        if parsed.path.startswith("/audio/"):
            name = Path(parsed.path.replace("/audio/", "", 1)).name
            path = resolve_audio_path(name)
            if not path:
                try:
                    path = hydrate_remote_audio(name)
                except Exception:
                    path = None
            if not path:
                status, body, ctype = make_json({"ok": False, "error": "Audio not found"}, 404)
                self.send_payload(status, body, ctype)
                return
            data = path.read_bytes()
            ctype = mimetypes.guess_type(str(path))[0] or "audio/mpeg"
            self.send_payload(200, data, ctype)
            return

        status, body, ctype = make_json({"ok": False, "error": "Not found"}, 404)
        self.send_payload(status, body, ctype)

    def do_POST(self):
        if self.reject_untrusted_request():
            return
        parsed = urlparse(self.path)
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if length < 0:
                raise ValueError("Invalid Content-Length")
            if length > MAX_REQUEST_BYTES:
                status, body, ctype = make_json({"ok": False, "error": f"Request is too large (max {MAX_REQUEST_BYTES // 1024 // 1024} MB)"}, 413)
                self.send_payload(status, body, ctype)
                return
            payload = read_json_bytes(self.rfile.read(length))
            if not isinstance(payload, dict):
                raise ValueError("JSON body must be an object")

            if parsed.path == "/api/tts":
                filename = asyncio.run(synthesize_mp3(
                    payload.get("text", ""),
                    payload.get("voice", "vi-VN-HoaiMyNeural"),
                    payload.get("rate", 1.0),
                ))
                request_host = self.headers.get("Host") or f"{HOST}:{PORT}"
                status, body, ctype = make_json({"ok": True, "audio_url": f"http://{request_host}/audio/{filename}"})
                self.send_payload(status, body, ctype)
                return

            if parsed.path == "/api/extract_pdf":
                raw = base64.b64decode(payload.get("data_base64", ""), validate=True)
                result = extract_pdf_bytes(
                    raw,
                    max_pages=int(payload.get("max_pages") or 0),
                    force_ocr=bool(payload.get("force_ocr") or False),
                )
                status, body, ctype = make_json({"ok": True, **result})
                self.send_payload(status, body, ctype)
                return

            if parsed.path == "/api/extract_docx":
                raw = base64.b64decode(payload.get("data_base64", ""), validate=True)
                result = extract_docx_bytes(raw)
                status, body, ctype = make_json({"ok": True, **result})
                self.send_payload(status, body, ctype)
                return

            if parsed.path == "/api/progress":
                result = write_device_progress(payload)
                status, body, ctype = make_json(result)
                self.send_payload(status, body, ctype)
                return

            if parsed.path == "/api/project_patch":
                result = write_project_patch(payload.get("doc") or {}, payload.get("activeDocId") or "")
                schedule_auto_start_next_project_prepare(delay=1.5)
                status, body, ctype = make_json(result)
                self.send_payload(status, body, ctype)
                return

            if parsed.path == "/api/projects":
                store = write_project_store(payload)
                schedule_auto_start_next_project_prepare(delay=1.5)
                status, body, ctype = make_json({"ok": True, **store})
                self.send_payload(status, body, ctype)
                return

            if parsed.path == "/api/project_prepare":
                job = start_background_project_prepare(
                    payload.get("doc") or {},
                    payload.get("jobs") or [],
                    audio_signature=payload.get("audioSignature") or "",
                    active_doc_id=payload.get("activeDocId") or "",
                    force=bool(payload.get("force") or False),
                    auto_batch=bool(payload.get("autoBatch") or False),
                )
                status, body, ctype = make_json({"ok": True, "job": job})
                self.send_payload(status, body, ctype)
                return

            if parsed.path == "/api/project_auto_start":
                job = auto_start_next_project_prepare(payload.get("previousDocId") or "")
                status, body, ctype = make_json({"ok": True, "job": job, "jobs": job_snapshot()})
                self.send_payload(status, body, ctype)
                return

            if parsed.path == "/api/cloud/sync_project":
                result = sync_project_to_cloud(payload.get("docId") or payload.get("id") or "")
                status, body, ctype = make_json(result, 200 if result.get("ok") else 400)
                self.send_payload(status, body, ctype)
                return

            if parsed.path == "/api/cloud/sync_all":
                result = sync_all_projects_to_cloud()
                status, body, ctype = make_json(result, 200 if result.get("ok") else 207)
                self.send_payload(status, body, ctype)
                return

            if parsed.path == "/api/cloud/sync_all_to_r2":
                result = sync_everything_to_r2()
                status, body, ctype = make_json(result, 200 if result.get("ok") else 400)
                self.send_payload(status, body, ctype)
                return

            if parsed.path == "/api/cloud/rebuild_library":
                result = rebuild_cloud_library()
                status, body, ctype = make_json(result, 200 if result.get("ok") else 400)
                self.send_payload(status, body, ctype)
                return

            if parsed.path in ("/api/cloud/delete_project", "/api/cloud/delete_projects"):
                doc_ids = payload.get("docIds")
                if doc_ids is None:
                    doc_ids = payload.get("docId") or payload.get("id") or ""
                result = delete_projects_from_cloud(doc_ids)
                status, body, ctype = make_json(result, 200 if result.get("ok") else 400)
                self.send_payload(status, body, ctype)
                return

            status, body, ctype = make_json({"ok": False, "error": "Not found"}, 404)
            self.send_payload(status, body, ctype)
        except (ValueError, binascii.Error, json.JSONDecodeError) as exc:
            status, body, ctype = make_json({"ok": False, "error": str(exc)}, 400)
            self.send_payload(status, body, ctype)
        except Exception as exc:
            traceback.print_exc()
            status, body, ctype = make_json({"ok": False, "error": str(exc)}, 500)
            self.send_payload(status, body, ctype)


def main():
    print(f"Local Reader server: http://{HOST}:{PORT}")
    print(f"Audio cache: {AUDIO_DIR}")
    print(f"VieNeu worker: {VIENEU_PYTHON}")
    print(f"Tesseract: {TESSERACT_EXE if TESSERACT_EXE.exists() else 'missing'}")
    print("Keep this window running while using VieNeu TTS/DOCX/PDF text.")
    SERVER_SHUTDOWN_EVENT.clear()
    server = LocalReaderHTTPServer((BIND_HOST, PORT), Handler)
    try:
        if VIENEU_ENABLED:
            warm_vieneu_workers_background()
        start_auto_project_watchdog()
        if CLOUD_ENABLED:
            schedule_cloud_delete_retry(read_cloud_deleted_doc_ids())
            schedule_reset_cloud_cleanup_background(read_project_store(), delay=5.0, force=True)
            schedule_r2_orphan_cleanup_background(delay=7.0, force=True)
        schedule_auto_start_next_project_prepare(delay=3.0)
        server.serve_forever()
    finally:
        try:
            server.server_close()
        finally:
            stop_vieneu_workers()


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit(0)

