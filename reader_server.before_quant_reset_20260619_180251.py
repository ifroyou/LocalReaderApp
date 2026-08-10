import asyncio
import base64
import hashlib
import io
import json
import mimetypes
import os
import re
import subprocess
import sys
import threading
import time
import traceback
import wave
import zipfile
from array import array
from concurrent.futures import ThreadPoolExecutor, as_completed
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import shutil
from urllib.parse import urlparse
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET

import fitz
import pytesseract
from PIL import Image, ImageOps
import hashlib
import hmac
import urllib.request
from datetime import datetime, timezone
from urllib.parse import quote


BIND_HOST = "0.0.0.0"
HOST = "127.0.0.1"
PORT = 8765
ROOT = Path(__file__).resolve().parent
AUDIO_DIR = ROOT / "reader_audio_cache"
PROJECT_STORE = ROOT / "reader_project_store.json"
R2_CONFIG_FILE = ROOT / "r2_config.json"
SUPABASE_CONFIG_FILE = ROOT / "supabase_config.disabled.json"
CLOUD_INDEX_FILE = ROOT / "cloud_sync_index.json"
R2_CLOUD_INDEX_FILE = ROOT / "r2_cloud_sync_index.json"
CLOUD_DELETED_DOC_IDS_FILE = ROOT / "cloud_deleted_doc_ids.json"
CLOUD_STORAGE_LIMIT_BYTES = 50 * 1024 * 1024 * 1024
SUPABASE_CLOUD_LIMIT_BYTES = 1 * 1024 * 1024 * 1024
R2_DEFAULT_CLOUD_LIMIT_BYTES = CLOUD_STORAGE_LIMIT_BYTES
LOCALAPPDATA = os.environ.get("LOCALAPPDATA", "")
RUNTIME_ROOT = Path(os.environ.get("LOCAL_READER_RUNTIME_DIR") or (Path(LOCALAPPDATA) / "LocalReaderApp" if LOCALAPPDATA else ROOT / ".runtime")).resolve()
TESSDATA_DIR = Path(os.environ.get("LOCAL_READER_TESSDATA_DIR") or (ROOT / "tessdata"))
TESSERACT_EXE = Path(os.environ.get("LOCAL_READER_TESSERACT_EXE") or shutil.which("tesseract") or r"C:\Program Files\Tesseract-OCR\tesseract.exe")
LOCAL_HF_HOME = Path(os.environ.get("LOCAL_READER_HF_HOME") or ((ROOT / ".hf_cache") if (ROOT / ".hf_cache").exists() else (RUNTIME_ROOT / "hf_cache")))
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

AUDIO_DIR.mkdir(exist_ok=True)

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
PROJECT_LOCK = threading.RLock()
BACKGROUND_LOCK = threading.Lock()
BACKGROUND_JOBS = {}
BACKGROUND_MAX_WORKERS = env_int("LOCAL_READER_BACKGROUND_WORKERS", len(VIENEU_PORTS), 1, 8)
BACKGROUND_PROJECT_SEMAPHORE = threading.Semaphore(1)
AUTO_PROJECT_CHAIN_LOCK = threading.Lock()
AUTO_PROJECT_CHAIN_IN_FLIGHT = False
AUTO_PROJECT_CHAIN_STARTED_TS = 0.0
CLOUD_AUTO_SYNC_LOCK = threading.Lock()
CLOUD_AUTO_SYNC_IN_FLIGHT = set()
CLOUD_SYNC_SERIAL_LOCK = threading.Lock()
CLOUD_LIBRARY_REBUILD_LOCK = threading.Lock()
CLOUD_LIBRARY_REBUILD_QUEUED = False
VIENEU_LEGACY_MODEL_VERSION = "vieneu-tts-v2-neucodec-int8"
VIENEU_MODEL_VERSION = os.environ.get("LOCAL_READER_VIENEU_MODEL_VERSION") or TTS_ENGINE_VERSION
ALLOW_LEGACY_VIENEU_CACHE = VIENEU_MODEL_VERSION == VIENEU_LEGACY_MODEL_VERSION
VIENEU_AUDIO_SPEED = "1.0x"
AUTO_PROJECT_WATCHDOG_ENABLED = env_flag("LOCAL_READER_AUTO_PROJECT_WATCHDOG", True)
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


def read_project_store():
    with PROJECT_LOCK:
        if not PROJECT_STORE.exists():
            return {"docs": [], "activeDocId": ""}
        try:
            data = json.loads(PROJECT_STORE.read_text(encoding="utf-8"))
            return {
                "docs": data.get("docs") if isinstance(data.get("docs"), list) else [],
                "activeDocId": data.get("activeDocId") or "",
                "savedAt": data.get("savedAt") or "",
            }
        except Exception:
            return {"docs": [], "activeDocId": ""}


def write_project_store(payload):
    with PROJECT_LOCK:
        deleted_doc_ids = clean_doc_ids(payload.get("deletedDocIds") if isinstance(payload.get("deletedDocIds"), list) else [])
        if deleted_doc_ids:
            remember_cloud_deleted_doc_ids(deleted_doc_ids)
            with BACKGROUND_LOCK:
                for deleted_doc_id in deleted_doc_ids:
                    BACKGROUND_JOBS.pop(deleted_doc_id, None)
        data = {
            "docs": payload.get("docs") if isinstance(payload.get("docs"), list) else [],
            "activeDocId": payload.get("activeDocId") or "",
            "savedAt": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "deletedDocIds": deleted_doc_ids,
        }
        data = sanitize_project_store_for_write(data)
        data.pop("deletedDocIds", None)
        tmp = PROJECT_STORE.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        last_error = None
        for attempt in range(8):
            try:
                tmp.replace(PROJECT_STORE)
                last_error = None
                break
            except PermissionError as exc:
                last_error = exc
                time.sleep(0.12 * (attempt + 1))
        if last_error:
            raise last_error
        if os.name == "nt":
            try:
                subprocess.run(["attrib", "+h", str(PROJECT_STORE)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=3)
            except Exception:
                pass
        return data


def audio_url(filename):
    return f"/audio/{filename}"


def load_r2_config():
    config = {}
    if R2_CONFIG_FILE.exists():
        try:
            config.update(json.loads(R2_CONFIG_FILE.read_text(encoding="utf-8")))
        except Exception:
            pass
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
    has_api_token = bool(str(config.get("api_token") or "").strip())
    has_s3_keys = bool(str(config.get("access_key_id") or "").strip() and str(config.get("secret_access_key") or "").strip())
    if not has_api_token and not has_s3_keys:
        missing.extend(["api_token_or_s3_keys"])
    return missing


def r2_object_key(config, rel_path):
    rel = str(rel_path or "").replace("\\", "/").strip("/")
    prefix = str(config.get("prefix") or "").strip("/")
    return f"{prefix}/{rel}".strip("/") if prefix else rel


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


def r2_put_object(config, object_key, data, content_type):
    account_id = str(config.get("account_id") or "").strip()
    access_key = str(config.get("access_key_id") or "").strip()
    secret_key = str(config.get("secret_access_key") or "").strip()
    bucket = str(config.get("bucket") or "").strip()
    if not (account_id and access_key and secret_key and bucket):
        return r2_api_put_object(config, object_key, data, content_type)
    host = f"{account_id}.r2.cloudflarestorage.com"
    encoded_key = "/".join(quote(part, safe="") for part in str(object_key).split("/"))
    canonical_uri = f"/{quote(bucket, safe='')}/{encoded_key}"
    url = f"https://{host}{canonical_uri}"
    payload_hash = hashlib.sha256(data).hexdigest()
    now = datetime.now(timezone.utc)
    amz_date = now.strftime("%Y%m%dT%H%M%SZ")
    date_stamp = now.strftime("%Y%m%d")
    headers = {
        "Content-Type": content_type,
        "Host": host,
        "x-amz-content-sha256": payload_hash,
        "x-amz-date": amz_date,
    }
    signed_headers = "content-type;host;x-amz-content-sha256;x-amz-date"
    canonical_headers = (
        f"content-type:{content_type}\n"
        f"host:{host}\n"
        f"x-amz-content-sha256:{payload_hash}\n"
        f"x-amz-date:{amz_date}\n"
    )
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
    headers["Authorization"] = f"AWS4-HMAC-SHA256 Credential={access_key}/{scope}, SignedHeaders={signed_headers}, Signature={signature}"
    request = urllib.request.Request(url, data=data, headers=headers, method="PUT")
    with urllib.request.urlopen(request, timeout=120) as response:
        return response.status


def read_r2_cloud_index():
    if R2_CLOUD_INDEX_FILE.exists():
        try:
            data = json.loads(R2_CLOUD_INDEX_FILE.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                data.setdefault("docs", [])
                data.setdefault("provider", "r2")
                data.setdefault("limitBytes", r2_cloud_limit_bytes())
                return data
        except Exception:
            pass
    return {"version": 1, "provider": "r2", "limitBytes": r2_cloud_limit_bytes(), "docs": []}


def write_r2_cloud_index(index):
    data = {
        "version": 1,
        "provider": "r2",
        "updatedAt": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "limitBytes": r2_cloud_limit_bytes(),
        "docs": index.get("docs") if isinstance(index.get("docs"), list) else [],
    }
    last_error = None
    payload = json.dumps(data, ensure_ascii=False, indent=2)
    for attempt in range(12):
        tmp = R2_CLOUD_INDEX_FILE.with_name(f"{R2_CLOUD_INDEX_FILE.stem}.{os.getpid()}.{threading.get_ident()}.{time.time_ns()}.tmp")
        try:
            tmp.write_text(payload, encoding="utf-8")
            tmp.replace(R2_CLOUD_INDEX_FILE)
            last_error = None
            break
        except PermissionError as exc:
            last_error = exc
            try:
                tmp.unlink(missing_ok=True)
            except Exception:
                pass
            time.sleep(0.25 + attempt * 0.15)
    if last_error:
        raise last_error
    try:
        os.system(f'attrib +h "{R2_CLOUD_INDEX_FILE}" >nul 2>nul')
    except Exception:
        pass
    return data


def r2_cloud_index_total_bytes(index):
    return sum(int(item.get("bytes") or 0) for item in (index.get("docs") or []))


def prune_r2_cloud_index(config, protected_doc_id=""):
    limit = r2_cloud_limit_bytes(config)
    index = read_r2_cloud_index()
    docs = index.get("docs") or []
    deleted = []
    while r2_cloud_index_total_bytes({"docs": docs}) > limit:
        candidates = [item for item in docs if item.get("docId") != protected_doc_id]
        if not candidates:
            break
        oldest = sorted(candidates, key=lambda item: item.get("syncedAt") or "")[0]
        for object_key in oldest.get("objectPaths") or []:
            r2_delete_object(config, object_key)
        docs = [item for item in docs if item.get("docId") != oldest.get("docId")]
        deleted.append({"docId": oldest.get("docId"), "title": oldest.get("title"), "bytes": int(oldest.get("bytes") or 0)})
    index["docs"] = docs
    write_r2_cloud_index(index)
    return deleted


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
        if meta and (not doc_id or doc_id not in meta):
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
            audio_item
            for audio_item in (doc.get("audioItems") if isinstance(doc.get("audioItems"), list) else [])
            if isinstance(audio_item, dict) and audio_item.get("text") and audio_item.get("url")
        ]
        if not audio_items:
            continue
        doc["audioItems"] = audio_items
        doc["text"] = "\n\n".join(str(audio_item.get("text") or "") for audio_item in audio_items).strip()
        library_docs.append(doc)
    payload = {"version": 1, "updatedAt": time.strftime("%Y-%m-%dT%H:%M:%S"), "app": "Local Reader Cloud Library", "provider": "r2", "limitBytes": r2_cloud_limit_bytes(config), "usedBytes": r2_cloud_index_total_bytes(index), "docs": library_docs}
    data = json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")
    r2_put_object(config, r2_object_key(config, "library.json"), data, "application/json; charset=utf-8")
    return r2_public_url(config, "library.json")


def upload_r2_player_assets(config):
    for player_name in ("cloud_player.html", "mobile_player.html"):
        player = ROOT / player_name
        if player.exists():
            r2_put_object(config, r2_object_key(config, player_name), player.read_bytes(), "text/html; charset=utf-8")
    cloud_mobile = ROOT / "CloudMobilePlayer" / "index.html"
    if cloud_mobile.exists():
        r2_put_object(config, r2_object_key(config, "CloudMobilePlayer/index.html"), cloud_mobile.read_bytes(), "text/html; charset=utf-8")


def update_r2_cloud_index_after_sync(config, record, rebuild_library=True):
    index = read_r2_cloud_index()
    docs = [item for item in (index.get("docs") or []) if item.get("docId") != record.get("docId")]
    docs.append(record)
    index["docs"] = docs
    write_r2_cloud_index(index)
    deleted = []
    library_url = r2_public_url(config, "library.json")
    if rebuild_library:
        deleted = prune_r2_cloud_index(config, protected_doc_id=record.get("docId") or "")
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


def read_cloud_deleted_doc_ids():
    if CLOUD_DELETED_DOC_IDS_FILE.exists():
        try:
            data = json.loads(CLOUD_DELETED_DOC_IDS_FILE.read_text(encoding="utf-8-sig"))
            ids = data.get("docIds") if isinstance(data, dict) else data
            return set(clean_doc_ids(ids if isinstance(ids, list) else []))
        except Exception:
            return set()
    return set()


def write_cloud_deleted_doc_ids(ids):
    cleaned = sorted(clean_doc_ids(list(ids or [])))
    tmp = CLOUD_DELETED_DOC_IDS_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps({"version": 1, "updatedAt": time.strftime("%Y-%m-%dT%H:%M:%S"), "docIds": cleaned}, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(CLOUD_DELETED_DOC_IDS_FILE)
    try:
        os.system(f'attrib +h "{CLOUD_DELETED_DOC_IDS_FILE}" >nul 2>nul')
    except Exception:
        pass
    return set(cleaned)


def remember_cloud_deleted_doc_ids(doc_ids):
    ids = clean_doc_ids(doc_ids)
    if not ids:
        return read_cloud_deleted_doc_ids()
    deleted = read_cloud_deleted_doc_ids()
    deleted.update(ids)
    return write_cloud_deleted_doc_ids(deleted)


def cloud_doc_id_is_deleted(doc_id):
    doc_id = str(doc_id or "").strip()
    return bool(doc_id and doc_id in read_cloud_deleted_doc_ids())


def delete_projects_from_r2(doc_ids):
    config = load_r2_config()
    missing = r2_missing_fields(config)
    if missing:
        return {"ok": False, "configured": False, "provider": "r2", "missing": missing, "error": "Missing R2 config fields"}
    ids = clean_doc_ids(doc_ids)
    index = read_r2_cloud_index()
    docs = index.get("docs") if isinstance(index.get("docs"), list) else []
    if not ids:
        library_url = upload_r2_cloud_library(config, index)
        return {"ok": True, "configured": True, "provider": "r2", "deleted": [], "deleted_objects": 0, "delete_errors": [], "library_url": library_url, "used_bytes": r2_cloud_index_total_bytes(index), "limit_bytes": r2_cloud_limit_bytes(config)}
    target_ids = set(ids)
    remaining = []
    deleted = []
    deleted_objects = 0
    delete_errors = []
    for item in docs:
        item_doc = item.get("doc") if isinstance(item.get("doc"), dict) else {}
        doc_id = str(item.get("docId") or item_doc.get("id") or "").strip()
        if doc_id not in target_ids:
            remaining.append(item)
            continue
        object_paths = []
        seen_paths = set()
        for object_key in (item.get("objectPaths") or []):
            object_key = str(object_key or "").strip("/")
            if object_key and object_key not in seen_paths:
                seen_paths.add(object_key)
                object_paths.append(object_key)
        for object_key in object_paths:
            try:
                r2_delete_object(config, object_key)
                deleted_objects += 1
            except Exception as exc:
                delete_errors.append({"docId": doc_id, "object": object_key, "error": str(exc)})
        deleted.append({"docId": doc_id, "title": item.get("title") or item_doc.get("title") or "", "objects": len(object_paths), "bytes": int(item.get("bytes") or 0)})
    index["docs"] = remaining
    index = write_r2_cloud_index(index)
    library_url = upload_r2_cloud_library(config, index)
    return {"ok": True, "configured": True, "provider": "r2", "requested": ids, "deleted": deleted, "deleted_objects": deleted_objects, "delete_errors": delete_errors, "used_bytes": r2_cloud_index_total_bytes(index), "limit_bytes": r2_cloud_limit_bytes(config), "library_url": library_url, "player_url": r2_public_url(config, "cloud_player.html"), "mobile_player_url": r2_public_url(config, "CloudMobilePlayer/index.html")}


def sync_project_to_r2(doc_id, rebuild_library=True, upload_player_assets=True):
    doc_id = str(doc_id or "").strip()
    if cloud_doc_id_is_deleted(doc_id):
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
        audio_path = AUDIO_DIR / name
        if not name or not audio_path.exists() or audio_path.stat().st_size <= 0:
            missing_audio.append(name or str(local_url))
            continue
        rel = f"projects/{doc_id}/audio/{name}"
        object_key = r2_object_key(config, rel)
        size = audio_path.stat().st_size
        ctype = mimetypes.guess_type(str(audio_path))[0] or "audio/wav"
        if object_key in previous_object_paths:
            reused_audio += 1
            reused_details.append({"name": name, "bytes": size})
        else:
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
    cloud_doc = {"id": doc.get("id"), "title": doc.get("title") or "File ??c", "folderPath": doc.get("folderPath") or "", "sourceName": doc.get("sourceName") or "", "text": cloud_text, "currentIndex": doc.get("currentIndex") or 0, "currentPartIndex": doc.get("currentPartIndex") or 0, "desktopOrder": desktop_order, "audioManifest": cloud_manifest, "audioItems": cloud_audio_items, "audioVoice": audio_meta["audioVoice"], "audioVoiceLabel": audio_meta["audioVoiceLabel"], "audioEngine": audio_meta["audioEngine"], "audioSpeed": doc.get("audioSpeed") or VIENEU_AUDIO_SPEED, "audioModelVersion": audio_meta["audioModelVersion"]}
    payload = {"version": 1, "syncedAt": time.strftime("%Y-%m-%dT%H:%M:%S"), "app": "Local Reader Cloud", "provider": "r2", "doc": cloud_doc, "missingAudio": missing_audio}
    manifest_rel = f"projects/{doc_id}/manifest.json"
    manifest_object_key = r2_object_key(config, manifest_rel)
    payload_bytes = json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")
    r2_put_object(config, manifest_object_key, payload_bytes, "application/json; charset=utf-8")
    object_paths.append(manifest_object_key)
    uploaded_bytes += len(payload_bytes)
    total_project_bytes += len(payload_bytes)
    r2_put_object(config, r2_object_key(config, "latest.json"), payload_bytes, "application/json; charset=utf-8")
    if upload_player_assets:
        upload_r2_player_assets(config)
    if cloud_doc_id_is_deleted(doc_id):
        for object_key in object_paths:
            try:
                r2_delete_object(config, object_key)
            except Exception:
                pass
        index = read_r2_cloud_index()
        index["docs"] = [item for item in (index.get("docs") or []) if item.get("docId") != doc_id]
        index = write_r2_cloud_index(index)
        library_url = upload_r2_cloud_library(config, index)
        return {"ok": True, "configured": True, "provider": "r2", "skipped": True, "deleted": True, "docId": doc_id, "deleted_objects": len(object_paths), "used_bytes": r2_cloud_index_total_bytes(index), "limit_bytes": r2_cloud_limit_bytes(config), "library_url": library_url}
    record = {"docId": doc.get("id"), "title": doc.get("title") or "File ??c", "folderPath": doc.get("folderPath") or "", "desktopOrder": desktop_order, "syncedAt": time.strftime("%Y-%m-%dT%H:%M:%S"), "bytes": total_project_bytes, "audioCount": uploaded_audio + reused_audio, "uploadedAudioCount": uploaded_audio, "reusedAudioCount": reused_audio, "objectPaths": object_paths, "manifestUrl": r2_public_url(config, manifest_rel), "doc": cloud_doc}
    index, deleted_old, library_url = update_r2_cloud_index_after_sync(config, record, rebuild_library=rebuild_library)
    return {"ok": True, "configured": True, "provider": "r2", "uploaded_audio": uploaded_audio, "reused_audio": reused_audio, "audio_count": uploaded_audio + reused_audio, "uploaded_bytes": uploaded_bytes, "project_bytes": total_project_bytes, "uploaded_details": uploaded_details[:20], "reused_details": reused_details[:20], "used_bytes": r2_cloud_index_total_bytes(index), "limit_bytes": r2_cloud_limit_bytes(config), "deleted_old": deleted_old, "missing_audio": missing_audio, "manifest_url": r2_public_url(config, manifest_rel), "latest_url": r2_public_url(config, "latest.json"), "library_url": library_url, "player_url": r2_public_url(config, "cloud_player.html"), "mobile_player_url": r2_public_url(config, "CloudMobilePlayer/index.html"), "public_base_url": str(config.get("public_base_url") or ""), "prefix": str(config.get("prefix") or "")}


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
            results.append({"docId": doc.get("id"), "title": doc.get("title"), "uploaded_audio": result.get("uploaded_audio"), "reused_audio": result.get("reused_audio"), "audio_count": result.get("audio_count"), "uploaded_bytes": result.get("uploaded_bytes"), "project_bytes": result.get("project_bytes"), "deleted_old": result.get("deleted_old") or [], "missing_audio": result.get("missing_audio") or []})
        except Exception as exc:
            errors.append({"docId": doc.get("id"), "title": doc.get("title"), "error": str(exc)})
    index = read_r2_cloud_index()
    prune_r2_cloud_index(config)
    index = read_r2_cloud_index()
    library_url = upload_r2_cloud_library(config, index)
    upload_r2_player_assets(config)
    return {"ok": not errors, "configured": True, "provider": "r2", "synced": results, "skipped": skipped, "errors": errors, "used_bytes": r2_cloud_index_total_bytes(index), "limit_bytes": r2_cloud_limit_bytes(config), "library_url": library_url, "player_url": r2_public_url(config, "cloud_player.html"), "mobile_player_url": r2_public_url(config, "CloudMobilePlayer/index.html")}


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
        audio_path = AUDIO_DIR / name
        if not name or not audio_path.exists() or audio_path.stat().st_size <= 0:
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
        "title": doc.get("title") or "File Ä‘á»c",
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
        "title": doc.get("title") or "File Ä‘á»c",
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
    return "r2"


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
    return {
        "configured": not missing,
        "missing": missing,
        "used_bytes": r2_cloud_index_total_bytes(read_r2_cloud_index()),
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
        return r2_cloud_index_total_bytes(read_r2_cloud_index())
    return cloud_index_total_bytes(read_cloud_index())


def sync_project_to_cloud(doc_id):
    doc_id = str(doc_id or "").strip()
    with CLOUD_SYNC_SERIAL_LOCK:
        if cloud_doc_id_is_deleted(doc_id):
            return {"ok": True, "configured": True, "provider": "r2", "skipped": True, "deleted": True, "docId": doc_id, "message": "Project was deleted from cloud, skip auto sync"}
        return sync_project_to_r2(doc_id)


def sync_all_projects_to_cloud():
    return sync_all_projects_to_r2()


def sync_everything_to_r2():
    return sync_all_projects_to_r2()


def rebuild_cloud_library():
    with CLOUD_SYNC_SERIAL_LOCK:
        config = load_r2_config()
        missing = r2_missing_fields(config)
        if missing:
            return {"ok": False, "configured": False, "provider": "r2", "missing": missing, "error": "Missing R2 config fields"}
        index = read_r2_cloud_index()
        library_url = upload_r2_cloud_library(config, index)
        return {"ok": True, "configured": True, "provider": "r2", "library_url": library_url, "used_bytes": r2_cloud_index_total_bytes(index), "limit_bytes": r2_cloud_limit_bytes(config)}


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


def delete_projects_from_cloud(doc_ids):
    ids = clean_doc_ids(doc_ids)
    remember_cloud_deleted_doc_ids(ids)
    return delete_projects_from_r2(ids)


def cloud_status_payload():
    r2 = r2_status_payload()
    return {
        "ok": r2["configured"],
        "configured": r2["configured"],
        "provider": "r2",
        **r2,
        "message": "" if r2["configured"] else "Cloudflare R2 chua cau hinh day du; khong ghi cloud.",
    }


def audio_url_is_cached(value):
    if not value:
        return False
    name = Path(str(value).rsplit("/audio/", 1)[-1]).name
    path = AUDIO_DIR / name
    return path.exists() and path.stat().st_size > 0


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
    incoming_marker = str(incoming.get("contentEditedAt") or "").strip()
    if not incoming_marker:
        return False
    existing_marker = str(existing.get("contentEditedAt") or "").strip()
    return not existing_marker or incoming_marker >= existing_marker


def sanitize_project_store_for_write(data):
    existing_docs = {}
    try:
        if PROJECT_STORE.exists():
            existing_data = json.loads(PROJECT_STORE.read_text(encoding="utf-8"))
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
    for incoming in data.get("docs") if isinstance(data.get("docs"), list) else []:
        if not isinstance(incoming, dict):
            continue
        doc = dict(incoming)
        doc_id = str(doc.get("id") or "").strip()
        if doc_id:
            incoming_ids.add(doc_id)
        if doc_id and (doc_id in deleted_ids or cloud_doc_id_is_deleted(doc_id)):
            continue
        existing = existing_docs.get(doc_id) or {}
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
            and existing_id not in deleted_ids
            and not cloud_doc_id_is_deleted(existing_id)
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
        if active_doc_id:
            store["activeDocId"] = active_doc_id
        return write_project_store(store), merged



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
            if audio_signature:
                doc["audioSignature"] = audio_signature
                doc["prepareRequested"] = False
                doc["audioPreparedAt"] = time.strftime("%Y-%m-%dT%H:%M:%S")
            doc["updatedAt"] = time.strftime("%Y-%m-%dT%H:%M:%S")
            write_project_store(store)
            return doc
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
            doc["updatedAt"] = time.strftime("%Y-%m-%dT%H:%M:%S")
            write_project_store(store)
            return doc
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
        path = AUDIO_DIR / filename
        if path.exists() and path.stat().st_size > 0:
            return audio_url(filename)
    return ""


def cached_job_count(jobs, manifest):
    manifest = manifest if isinstance(manifest, dict) else {}
    return sum(1 for job in jobs if cached_audio_url_for_job(job, manifest))



def auto_sync_project_to_cloud_background(doc_id):
    doc_id = str(doc_id or "").strip()
    if not doc_id:
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
    def runner():
        auto_start_next_project_prepare(previous_doc_id)

    timer = threading.Timer(max(0.0, float(delay or 0)), runner)
    timer.daemon = True
    timer.start()


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
        with ThreadPoolExecutor(max_workers=workers) as executor:
            futures = [executor.submit(synth_one, job) for job in todo]
            for future in as_completed(futures):
                key, url = future.result()
                completed += 1
                update_project_audio(doc_id, {key: url}, None)
                set_job_state(
                    doc_id,
                    status="running",
                    done=completed,
                    total=total,
                    workers=workers,
                    message=f"Dang tao audio {completed}/{total}",
                    **audio_meta,
                )

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
    r"^\s*(ngu[o?]n|source|model|diarization|diar model|ch[e?] d[o?]|mode|denoise|vad|ghi ch[u?]|note|transcript)\b\s*[:\-]?",
    re.IGNORECASE,
)
TTS_SPEAKER_LINE_RE = re.compile(r"^\s*\[\s*speaker\s*\d+\s*\]\s*$", re.IGNORECASE)
TTS_TIME_PREFIX_RE = re.compile(r"^\s*\[\s*\d{1,2}:\d{2}(?::\d{2})?(?:\.\d+)?\s*\]\s*")
TTS_ACRONYM_MAP = {
    "AI": "?y ai",
    "ICT": "ai xi ti",
    "IPO": "ai pi ?",
    "CEO": "xi i ?",
    "CFO": "xi ?p ?",
    "COO": "xi ? ?",
    "GDP": "gi ?i pi",
    "FDI": "?p ?i ai",
    "USD": "?? la M?",
    "VND": "??ng",
    "MWG": "em v? gi?",
    "HSG": "h?t ?t gi?",
    "FPT": "?p pi ti",
    "MBS": "em bi ?t",
    "HOSE": "h? s?",
    "HNX": "h?t en ?ch",
    "UPCOM": "?p com",
}



def ensure_tts_pause_punctuation(text):
    value = clean_text(text or "")
    if not value:
        return ""
    if re.search(r"[.!?????;:,)]$", value):
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
    for page_no, page in enumerate(doc, start=1):
        text = clean_text(page.get_text("text") or "")
        if text:
            pages.append(f"[Page {page_no}]\n{text}")
    return "\n\n".join(pages).strip()


def render_page_for_ocr(page, dpi=220):
    zoom = dpi / 72
    pix = page.get_pixmap(matrix=fitz.Matrix(zoom, zoom), alpha=False)
    img = Image.frombytes("RGB", [pix.width, pix.height], pix.samples)
    img = ImageOps.grayscale(img)
    img = ImageOps.autocontrast(img)
    return img


def ocr_pdf(doc, max_pages=0, dpi=220):
    if not TESSERACT_EXE.exists():
        raise RuntimeError("Missing Tesseract at C:\\Program Files\\Tesseract-OCR\\tesseract.exe")
    if not TESSDATA_DIR.exists():
        raise RuntimeError("Missing tessdata folder next to reader_server.py")

    page_count = doc.page_count
    limit = page_count if not max_pages else min(page_count, max(1, int(max_pages)))
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
    with fitz.open(stream=pdf_bytes, filetype="pdf") as doc:
        page_count = doc.page_count
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


W_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
W_TEXT = f"{{{W_NS}}}t"
W_TAB = f"{{{W_NS}}}tab"
W_BR = f"{{{W_NS}}}br"
W_CR = f"{{{W_NS}}}cr"


def docx_xml_to_paragraphs(xml_bytes):
    root = ET.fromstring(xml_bytes)
    paragraphs = []
    for paragraph in root.iter(f"{{{W_NS}}}p"):
        parts = []
        for node in paragraph.iter():
            if node.tag == W_TEXT and node.text:
                parts.append(node.text)
            elif node.tag == W_TAB:
                parts.append("\t")
            elif node.tag in (W_BR, W_CR):
                parts.append("\n")
        text = clean_text("".join(parts))
        if text:
            paragraphs.append(text)
    return paragraphs


def extract_docx_bytes(docx_bytes):
    if not docx_bytes:
        raise ValueError("DOCX data is empty")

    with zipfile.ZipFile(io.BytesIO(docx_bytes)) as zf:
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

        paragraphs = []
        for name in ordered:
            try:
                paragraphs.extend(docx_xml_to_paragraphs(zf.read(name)))
            except ET.ParseError:
                continue

    text = clean_text("\n\n".join(paragraphs))
    return {
        "text": text,
        "method": "docx_xml",
        "parts": len(ordered),
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
        path = AUDIO_DIR / filename
        if path.exists() and path.stat().st_size > 0:
            return filename
    return None


def worker_json(path, payload=None, timeout=60, port=None):
    target_port = port or VIENEU_PORT
    url = f"http://{HOST}:{target_port}{path}"
    if payload is None:
        with urllib.request.urlopen(url, timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8"))
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=data,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


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
    port = port or VIENEU_PORT
    try:
        health = worker_json("/health", timeout=2, port=port)
        if health.get("ok"):
            return health
    except Exception:
        pass

    with VIENEU_LOCK:
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
        process = VIENEU_PROCESSES.get(port)
        if process is not None and process.poll() is not None:
            raise RuntimeError(f"VieNeu worker {port} exited with code {process.returncode}")
        try:
            health = worker_json("/health", timeout=3, port=port)
            if health.get("ok"):
                return health
            last_error = health.get("error") or health
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
        try:
            ensure_vieneu_workers(timeout=240)
        except Exception as exc:
            print(f"VieNeu warmup failed: {exc}", flush=True)
    threading.Thread(target=runner, name="warm-vieneu-workers", daemon=True).start()


if VIENEU_ENABLED:
    warm_vieneu_workers_background()

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
    text = clean_text(text)
    if not text:
        raise ValueError("Text is empty")
    if len(text) > 2600:
        text = text[:2600]
    voice = voice or VIENEU_DEFAULT_VOICE
    filename = vieneu_cache_filename(text, voice)
    out_path = AUDIO_DIR / filename
    if out_path.exists() and out_path.stat().st_size > 0:
        return filename
    ready_ports = ensure_vieneu_workers()
    tmp_path = out_path.with_suffix(".tmp.wav")
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


class Handler(BaseHTTPRequestHandler):
    server_version = "LocalReader/2.0"

    def log_message(self, fmt, *args):
        print("[%s] %s" % (self.log_date_time_string(), fmt % args))

    def end_headers(self):
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        super().end_headers()

    def send_payload(self, status, body, content_type):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_OPTIONS(self):
        self.send_response(204)
        self.end_headers()

    def do_GET(self):
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
                self.send_payload(200, index_path.read_bytes(), "text/html; charset=utf-8")
                return
            status, body, ctype = make_json({"ok": False, "error": "index.html not found"}, 404)
            self.send_payload(status, body, ctype)
            return

        if parsed.path == "/api/health":
            langs = sorted(p.stem for p in TESSDATA_DIR.glob("*.traineddata")) if TESSDATA_DIR.exists() else []
            status, body, ctype = make_json({
                "ok": True,
                "engine": "vieneu-tts",
                "port": PORT,
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
                "app_root": str(ROOT),
                "runtime_root": str(RUNTIME_ROOT),
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

        if parsed.path == "/api/projects":
            store = read_project_store()
            status, body, ctype = make_json({"ok": True, **store})
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
            path = AUDIO_DIR / name
            if not path.exists():
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
        parsed = urlparse(self.path)
        try:
            length = int(self.headers.get("Content-Length", "0"))
            payload = read_json_bytes(self.rfile.read(length))

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
                raw = base64.b64decode(payload.get("data_base64", ""))
                result = extract_pdf_bytes(
                    raw,
                    max_pages=int(payload.get("max_pages") or 0),
                    force_ocr=bool(payload.get("force_ocr") or False),
                )
                status, body, ctype = make_json({"ok": True, **result})
                self.send_payload(status, body, ctype)
                return

            if parsed.path == "/api/extract_docx":
                raw = base64.b64decode(payload.get("data_base64", ""))
                result = extract_docx_bytes(raw)
                status, body, ctype = make_json({"ok": True, **result})
                self.send_payload(status, body, ctype)
                return

            if parsed.path == "/api/projects":
                store = write_project_store(payload)
                schedule_auto_start_next_project_prepare(delay=1.5)
                rebuild_cloud_library_background(delay=1.0)
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
    start_auto_project_watchdog()
    schedule_auto_start_next_project_prepare(delay=3.0)
    ThreadingHTTPServer((BIND_HOST, PORT), Handler).serve_forever()


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit(0)

