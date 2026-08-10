import json
import os
import re
import secrets
import sys
import threading
import time
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

# Xet is occasionally blocked/reset on corporate and home Windows networks.
# The regular Hugging Face HTTP downloader is slower only on the first load,
# but considerably more reliable for this local app.
os.environ.setdefault("HF_HUB_DISABLE_XET", "1")


def lower_cpu_worker_priority():
    """Keep the UI responsive while ONNX uses the CPU heavily."""
    if os.name != "nt":
        return
    try:
        import ctypes
        below_normal_priority_class = 0x00004000
        handle = ctypes.windll.kernel32.GetCurrentProcess()
        ctypes.windll.kernel32.SetPriorityClass(handle, below_normal_priority_class)
    except Exception:
        pass


lower_cpu_worker_priority()


def configure_cuda_dll_paths():
    """Expose CUDA wheel DLLs before llama.cpp is imported."""
    sites = []
    try:
        sites.append(Path(sys.executable).resolve().parents[1] / "Lib" / "site-packages")
    except Exception:
        pass
    local_appdata = os.environ.get("LOCALAPPDATA")
    if local_appdata:
        sites.append(Path(local_appdata) / "LocalReaderApp" / ".vieneu_test" / "Lib" / "site-packages")

    seen = set()
    for site in sites:
        for rel in (
            "nvidia/cuda_runtime/bin",
            "nvidia/cublas/bin",
            "nvidia/cuda_nvrtc/bin",
            "llama_cpp/lib",
        ):
            path = (site / rel).resolve()
            if path in seen or not path.exists():
                continue
            seen.add(path)
            try:
                os.add_dll_directory(str(path))
            except Exception:
                pass
            os.environ["PATH"] = str(path) + os.pathsep + os.environ.get("PATH", "")


configure_cuda_dll_paths()
from vieneu import Vieneu


HOST = "127.0.0.1"
PORT = int(sys.argv[1]) if len(sys.argv) > 1 else int(os.environ.get("VIENEU_WORKER_PORT", "8766"))
WORKER_PROTOCOL_ID = "localreader-vieneu-v3turbo-2"
WORKER_TOKEN = (os.environ.get("LOCAL_READER_WORKER_TOKEN") or secrets.token_urlsafe(32)).strip()
MAX_REQUEST_BYTES = 1024 * 1024
ROOT = Path(__file__).resolve().parent
AUDIO_DIR = Path(os.environ.get("LOCAL_READER_AUDIO_DIR") or (ROOT / "reader_audio_cache")).resolve()
AUDIO_DIR.mkdir(parents=True, exist_ok=True)

MODEL = None
MODEL_ERROR = ""
MODEL_LOCK = threading.Lock()
VOICE_LIST = []
MODEL_INFO = {}


def cuda_info():
    try:
        import torch
        available = bool(torch.cuda.is_available())
        return {
            "torch": getattr(torch, "__version__", ""),
            "cuda_available": available,
            "torch_cuda": getattr(torch.version, "cuda", ""),
            "device": torch.cuda.get_device_name(0) if available else "",
        }
    except Exception as exc:
        return {
            "torch": "",
            "cuda_available": False,
            "torch_cuda": "",
            "device": "",
            "error": str(exc),
        }


def requested_device():
    value = (os.environ.get("LOCAL_READER_VIENEU_BACKBONE_DEVICE") or "auto").strip().lower()
    if value in ("cuda", "cpu", "mps"):
        return value
    if value != "auto":
        return value
    return "cuda" if cuda_info().get("cuda_available") else "cpu"


def model_attempts():
    mode = (os.environ.get("LOCAL_READER_VIENEU_MODE") or "standard").strip().lower() or "standard"
    repo = (os.environ.get("LOCAL_READER_VIENEU_BACKBONE_REPO") or "").strip()
    device = requested_device()
    backend = (os.environ.get("LOCAL_READER_VIENEU_BACKEND") or "auto").strip().lower() or "auto"
    if mode in ("v3", "v3turbo", "v3-turbo", "v3_turbo"):
        repo = repo or "pnnbao-ump/VieNeu-TTS-v3-Turbo"
        # ONNX is the lightweight, supported CPU backend for v3 Turbo. Resolve
        # it explicitly so a non-NVIDIA laptop never tries the PyTorch path.
        if device == "cpu" and backend == "auto":
            backend = "onnx"
        attempts = []
        kwargs = {"mode": "v3turbo", "device": device, "backend": backend, "backbone_repo": repo}
        attempts.append((f"v3turbo-{device}-{backend}", kwargs))
        if device != "cpu":
            attempts.append(("v3turbo-cpu-onnx-fallback", {
                "mode": "v3turbo",
                "device": "cpu",
                "backend": "onnx",
                "backbone_repo": repo,
            }))
        return attempts
    if device == "cuda" and not repo:
        repo = "pnnbao-ump/VieNeu-TTS-0.3B"
    model_format = (os.environ.get("LOCAL_READER_VIENEU_MODEL_FORMAT") or "gguf").strip().lower()
    full_fp32 = model_format in ("full", "full-fp32", "fp32", "pytorch", "torch", "safetensors", "model.safetensors")
    gguf_filename = (os.environ.get("LOCAL_READER_VIENEU_GGUF_FILENAME") or "").strip()
    if not full_fp32 and not gguf_filename and repo.rstrip("/").lower().endswith("vieneu-tts-0.3b"):
        gguf_filename = "VieNeu-TTS-0.3B-Q4_K_M.gguf"
    attempts = []

    if mode == "fast":
        kwargs = {"mode": "fast"}
        if repo:
            kwargs["backbone_repo"] = repo
        attempts.append(("fast", kwargs))

    if mode != "fast":
        kwargs = {"mode": "standard", "backbone_device": device}
        if repo:
            kwargs["backbone_repo"] = repo
        if full_fp32:
            kwargs["gguf_filename"] = None
        elif device == "cuda" and gguf_filename:
            kwargs["gguf_filename"] = gguf_filename
        label_suffix = "full-fp32" if full_fp32 else "gguf"
        attempts.append((f"standard-{device}-{label_suffix}", kwargs))
        if device != "cpu":
            cpu_kwargs = {"mode": "standard", "backbone_device": "cpu"}
            if repo:
                cpu_kwargs["backbone_repo"] = repo
            if full_fp32:
                cpu_kwargs["gguf_filename"] = None
            attempts.append((f"standard-cpu-fallback-{label_suffix}", cpu_kwargs))

    if not full_fp32:
        attempts.append(("standard-default", {"mode": "standard"}))
    deduped = []
    seen = set()
    for label, kwargs in attempts:
        key = json.dumps(kwargs, sort_keys=True)
        if key not in seen:
            deduped.append((label, kwargs))
            seen.add(key)
    return deduped


def clean_text(text):
    text = (text or "").replace("\r", "")
    text = re.sub(r"[\u00a0\u200b\u200c\u200d]", " ", text)
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r" *\n *", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def load_model():
    global MODEL, MODEL_ERROR, VOICE_LIST, MODEL_INFO
    os.environ.setdefault("PYTHONIOENCODING", "utf-8")
    os.environ.setdefault("HF_HUB_DISABLE_SYMLINKS_WARNING", "1")
    last_error = None
    attempts = model_attempts()
    for round_no in range(2):
        for label, kwargs in attempts:
            try:
                started = time.time()
                print(f"Loading VieNeu with {label}: {kwargs}", flush=True)
                model = Vieneu(**kwargs)
                MODEL = model
                VOICE_LIST = [
                    {"name": key, "display": desc}
                    for desc, key in model.list_preset_voices()
                ]
                MODEL_INFO = {
                    "label": label,
                    "kwargs": kwargs,
                    "cuda": cuda_info(),
                    "seconds": round(time.time() - started, 2),
                }
                MODEL_ERROR = ""
                print(f"VieNeu loaded in {MODEL_INFO['seconds']:.2f}s using {label}", flush=True)
                return
            except Exception as exc:
                last_error = exc
                MODEL_ERROR = str(exc)
                print(f"VieNeu load failed {label} round {round_no + 1}: {exc}", flush=True)
                time.sleep(2 + round_no * 2)
    raise RuntimeError(f"Cannot load VieNeu: {last_error}")


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


def safe_output_path(value):
    path = Path(value or "").resolve()
    if AUDIO_DIR not in path.parents and path != AUDIO_DIR:
        raise ValueError("Output path must be inside reader_audio_cache")
    if path.suffix.lower() != ".wav":
        raise ValueError("Output path must be .wav")
    return path


class Handler(BaseHTTPRequestHandler):
    server_version = "VieNeuWorker/3.2"

    def setup(self):
        super().setup()
        try:
            self.connection.settimeout(90)
        except Exception:
            pass

    def authorized(self):
        token = str(self.headers.get("X-Local-Reader-Token") or "")
        return bool(token and secrets.compare_digest(token, WORKER_TOKEN))

    def reject_unauthorized(self):
        if self.authorized():
            return False
        status, body, ctype = make_json({"ok": False, "error": "Unauthorized local worker request"}, 403)
        self.send_payload(status, body, ctype)
        return True

    def log_message(self, fmt, *args):
        print("[%s] %s" % (self.log_date_time_string(), fmt % args), flush=True)

    def end_headers(self):
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        super().end_headers()

    def send_payload(self, status, body, content_type):
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
        status, body, ctype = make_json({"ok": False, "error": "CORS is disabled"}, 403)
        self.send_payload(status, body, ctype)

    def do_GET(self):
        if self.reject_unauthorized():
            return
        parsed = urlparse(self.path)
        if parsed.path == "/health":
            status, body, ctype = make_json({
                "ok": MODEL is not None,
                "protocol": WORKER_PROTOCOL_ID,
                "engine": "vieneu-tts",
                "port": PORT,
                "error": MODEL_ERROR,
                "voices": VOICE_LIST,
                "model": MODEL_INFO,
            }, 200 if MODEL is not None else 503)
            self.send_payload(status, body, ctype)
            return
        if parsed.path == "/voices":
            status, body, ctype = make_json({
                "ok": MODEL is not None,
                "voices": VOICE_LIST,
                "error": MODEL_ERROR,
            }, 200 if MODEL is not None else 503)
            self.send_payload(status, body, ctype)
            return
        status, body, ctype = make_json({"ok": False, "error": "Not found"}, 404)
        self.send_payload(status, body, ctype)

    def do_POST(self):
        if self.reject_unauthorized():
            return
        parsed = urlparse(self.path)
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if length < 0 or length > MAX_REQUEST_BYTES:
                status, body, ctype = make_json({"ok": False, "error": "Worker request is too large"}, 413)
                self.send_payload(status, body, ctype)
                return
            payload = read_json_bytes(self.rfile.read(length))
            if not isinstance(payload, dict):
                raise ValueError("JSON body must be an object")
            if parsed.path == "/tts":
                if MODEL is None:
                    raise RuntimeError(MODEL_ERROR or "VieNeu model is not loaded")
                text = clean_text(payload.get("text", ""))
                voice_name = payload.get("voice_name") or payload.get("voice") or None
                output = safe_output_path(payload.get("output", ""))
                if not text:
                    raise ValueError("Text is empty")
                started = time.time()
                with MODEL_LOCK:
                    voice = MODEL.get_preset_voice(voice_name) if voice_name else None
                    audio = MODEL.infer(
                        text=text,
                        voice=voice,
                        temperature=float(payload.get("temperature") or 0.8),
                        top_k=int(payload.get("top_k") or 25),
                        top_p=float(payload.get("top_p") or 0.95),
                        repetition_penalty=float(payload.get("repetition_penalty") or 1.2),
                        max_chars=int(payload.get("max_chars") or 256),
                    )
                    MODEL.save(audio, str(output))
                status, body, ctype = make_json({
                    "ok": True,
                    "output": str(output),
                    "voice": voice_name,
                    "seconds": round(time.time() - started, 2),
                    "bytes": output.stat().st_size if output.exists() else 0,
                })
                self.send_payload(status, body, ctype)
                return
            status, body, ctype = make_json({"ok": False, "error": "Not found"}, 404)
            self.send_payload(status, body, ctype)
        except ValueError as exc:
            status, body, ctype = make_json({"ok": False, "error": str(exc)}, 400)
            self.send_payload(status, body, ctype)
        except Exception as exc:
            traceback.print_exc()
            status, body, ctype = make_json({"ok": False, "error": str(exc)}, 500)
            self.send_payload(status, body, ctype)


def main():
    load_model()
    print(f"VieNeu worker: http://{HOST}:{PORT}", flush=True)
    ThreadingHTTPServer((HOST, PORT), Handler).serve_forever()


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit(0)
