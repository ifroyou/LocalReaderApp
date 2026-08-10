import argparse
import os
import shutil
import subprocess
import sys
import venv
from pathlib import Path


APP_DIR = Path(__file__).resolve().parent
LOCALAPPDATA = os.environ.get("LOCALAPPDATA", "")
RUNTIME_ROOT = Path(os.environ.get("LOCAL_READER_RUNTIME_DIR") or (Path(LOCALAPPDATA) / "LocalReaderApp" if LOCALAPPDATA else APP_DIR / ".runtime")).resolve()
VENV_DIR = RUNTIME_ROOT / ".vieneu_test"
PYTHON_EXE = VENV_DIR / "Scripts" / "python.exe"


def run(cmd, **kwargs):
    print("+ " + " ".join(str(part) for part in cmd), flush=True)
    subprocess.check_call([str(part) for part in cmd], **kwargs)


def has_nvidia_gpu():
    if not shutil.which("nvidia-smi"):
        return False
    try:
        result = subprocess.run(["nvidia-smi"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=10)
        return result.returncode == 0
    except Exception:
        return False


def ensure_venv():
    RUNTIME_ROOT.mkdir(parents=True, exist_ok=True)
    if PYTHON_EXE.exists():
        return
    print(f"Creating runtime venv: {VENV_DIR}", flush=True)
    venv.EnvBuilder(with_pip=True, clear=False, upgrade_deps=False).create(VENV_DIR)


def install_packages(use_cuda):
    run([PYTHON_EXE, "-m", "pip", "install", "--upgrade", "pip", "setuptools", "wheel"])
    run([
        PYTHON_EXE,
        "-m",
        "pip",
        "install",
        "PyMuPDF",
        "pytesseract",
        "pillow",
        "vieneu==3.0.9",
    ])
    if use_cuda:
        run([
            PYTHON_EXE,
            "-m",
            "pip",
            "install",
            "torch",
            "torchaudio",
            "--index-url",
            "https://download.pytorch.org/whl/cu128",
        ])
        # Dependencies used by the v3 Turbo PyTorch/CUDA engine. The CPU
        # installation deliberately stays lightweight and uses ONNX only.
        run([
            PYTHON_EXE,
            "-m",
            "pip",
            "install",
            "transformers",
            "librosa>=0.11.0",
            "neucodec>=0.0.4",
            "accelerate",
        ])


def verify(use_cuda):
    code = r"""
import importlib.util
import sys
use_cuda = sys.argv[1] == "1"
names = ["fitz", "pytesseract", "PIL", "vieneu", "torch"]
if not use_cuda:
    names = ["fitz", "pytesseract", "PIL", "vieneu", "onnxruntime"]
else:
    names += ["soundfile", "transformers"]
for name in names:
    spec = importlib.util.find_spec(name)
    print(f"{name}: {'OK' if spec else 'MISSING'}")
try:
    import torch
    print("torch_version:", torch.__version__)
    print("cuda_available:", torch.cuda.is_available())
    if torch.cuda.is_available():
        print("cuda_device:", torch.cuda.get_device_name(0))
        print("torch_cuda:", torch.version.cuda)
except Exception as exc:
    print("torch_error:", exc)
"""
    run([PYTHON_EXE, "-c", code, "1" if use_cuda else "0"])


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--cpu", action="store_true", help="Install CPU torch even when NVIDIA is detected.")
    parser.add_argument("--skip-install", action="store_true", help="Only create/check the venv.")
    args = parser.parse_args()

    if sys.version_info < (3, 10):
        raise SystemExit("Python 3.10+ is required.")
    if sys.version_info >= (3, 13):
        print("Warning: Python 3.13+ may not be supported by every TTS dependency. Python 3.11 or 3.12 is safer.", flush=True)

    use_cuda = (not args.cpu) and has_nvidia_gpu()
    print(f"App: {APP_DIR}", flush=True)
    print(f"Runtime: {RUNTIME_ROOT}", flush=True)
    print(f"CUDA install: {'yes' if use_cuda else 'no'}", flush=True)

    ensure_venv()
    if not args.skip_install:
        install_packages(use_cuda)
    verify(use_cuda)

    print("", flush=True)
    print("Setup complete. Open Local Reader with Local Reader.exe.", flush=True)
    print("If VieNeu complains about eSpeak NG, install eSpeak NG on Windows, then open Local Reader again.", flush=True)


if __name__ == "__main__":
    main()
