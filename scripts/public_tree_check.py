"""Fail fast when a public release tree contains private or generated material."""

from __future__ import annotations

import re
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
REQUIRED_FILES = (
    "README.md",
    "LICENSE",
    "SECURITY.md",
    "CONTRIBUTING.md",
    "RELEASE.md",
    "RELEASE_NOTES_v0.1.0.md",
    "THIRD_PARTY_NOTICES.md",
    "index.html",
    "vendor/katex/katex.min.js",
    "vendor/katex/katex.min.css",
    "docs/local-first-flow.svg",
    "requirements-cpu.txt",
    "requirements-gpu.txt",
    "requirements-cpu.lock.txt",
    "requirements-gpu.lock.txt",
    "scripts/archive_check.py",
)

# These are intentionally high-confidence matches.  The check should stop an
# accidental credential commit without treating normal documentation as a key.
SECRET_PATTERNS = (
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    re.compile(r"\b(?:ghp|gho|ghu|ghs|ghr|github_pat)_[A-Za-z0-9_]{20,}\b"),
    re.compile(r"\bsk-[A-Za-z0-9]{20,}\b"),
    re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{20,}\b"),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
)

PRIVATE_OR_GENERATED_NAME = re.compile(
    r"(?:^|[._-])(?:before|backup|bak|tmp|temp|secret|private)(?:[._-]|$)|"
    r"(?:\.err(?:\.txt)?|\.log|\.swp|\.swo)$",
    re.IGNORECASE,
)

SKIP_DIRS = {
    ".git",
    ".venv",
    ".pytest_cache",
    ".mypy_cache",
    ".ruff_cache",
    "__pycache__",
    "build",
    "dist",
    "release",
    "reader_audio_cache",
    "reader_device_deletions",
    "reader_device_progress",
    "reader_device_stores",
    "reader_device_patches",
    "reader_r2_device_indexes",
    "reader_r2_device_removals",
}


def iter_public_files():
    for path in ROOT.rglob("*"):
        if not path.is_file():
            continue
        relative = path.relative_to(ROOT)
        if any(part in SKIP_DIRS for part in relative.parts):
            continue
        yield path, relative


def main() -> int:
    failures: list[str] = []
    for name in REQUIRED_FILES:
        if not (ROOT / name).is_file():
            failures.append(f"missing required public file: {name}")

    for path, relative in iter_public_files():
        if PRIVATE_OR_GENERATED_NAME.search(path.name):
            failures.append(f"private/generated filename: {relative}")
            continue
        try:
            data = path.read_bytes()
        except OSError as exc:
            failures.append(f"cannot read {relative}: {exc}")
            continue
        if b"\x00" in data:
            continue
        text = data.decode("utf-8", errors="ignore")
        for pattern in SECRET_PATTERNS:
            if pattern.search(text):
                failures.append(f"possible credential in {relative}: {pattern.pattern}")
                break

    if failures:
        print("Public tree check failed:", file=sys.stderr)
        for failure in failures:
            print(f"- {failure}", file=sys.stderr)
        return 1
    print("Public tree check passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
