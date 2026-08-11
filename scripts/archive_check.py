"""Verify that a generated source archive contains only public project material."""

from __future__ import annotations

import re
import sys
import zipfile
from pathlib import PurePosixPath


REQUIRED_FILES = {
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
    "requirements-cpu.lock.txt",
    "requirements-gpu.lock.txt",
}

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


def relative_name(name: str, prefix: str | None) -> str:
    parts = PurePosixPath(name).parts
    if prefix and parts and parts[0] == prefix:
        return "/".join(parts[1:])
    return name


def main() -> int:
    if len(sys.argv) != 2:
        print("usage: python scripts/archive_check.py ARCHIVE.zip", file=sys.stderr)
        return 2

    archive_path = sys.argv[1]
    failures: list[str] = []
    seen: set[str] = set()
    try:
        archive = zipfile.ZipFile(archive_path)
    except (OSError, zipfile.BadZipFile) as exc:
        print(f"Cannot read archive: {exc}", file=sys.stderr)
        return 2

    with archive:
        infos = [info for info in archive.infolist() if not info.is_dir()]
        first_parts = {
            PurePosixPath(info.filename).parts[0]
            for info in infos
            if PurePosixPath(info.filename).parts
        }
        prefix = next(iter(first_parts)) if len(first_parts) == 1 else None
        for info in infos:
            relative = relative_name(info.filename, prefix)
            seen.add(relative)
            path = PurePosixPath(relative)
            if any(part in SKIP_DIRS for part in path.parts):
                continue
            if PRIVATE_OR_GENERATED_NAME.search(path.name):
                failures.append(f"private/generated filename: {relative}")
                continue
            data = archive.read(info)
            if b"\x00" in data:
                continue
            text = data.decode("utf-8", errors="ignore")
            for pattern in SECRET_PATTERNS:
                if pattern.search(text):
                    failures.append(f"possible credential in {relative}: {pattern.pattern}")
                    break

    failures.extend(
        f"missing required archive file: {name}"
        for name in sorted(REQUIRED_FILES - seen)
    )
    if failures:
        print("Source archive check failed:", file=sys.stderr)
        for failure in failures:
            print(f"- {failure}", file=sys.stderr)
        return 1
    print(f"Source archive check passed: {archive_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
