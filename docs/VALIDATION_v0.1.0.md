# v0.1.0 validation record

Validation date: 2026-08-11

This record documents checks performed locally on Windows. It contains no
user documents, credentials, generated audio, or model cache.

## Clean install and automated checks

- `Setup Local Reader.bat` was run with an isolated `%LOCALAPPDATA%` directory
  and Python 3.11. The batch wrapper exited with code 0.
- The CPU runtime installed all 68 entries from
  `requirements-cpu.lock.txt`; `pip check` reported no broken requirements.
- `compileall`, `scripts/public_tree_check.py`, and the eight-test unittest
  suite passed.
- The installed runtime imported `pypdfium2`, `pytesseract`, `PIL`, `vieneu`,
  and `onnxruntime` successfully.

## Functional smoke tests

- A text-layer PDF returned one-based page labels and extracted text.
- An image-only PDF went through the local Vietnamese OCR path and returned
  `[Page 1]` plus the expected smoke-test text.
- DOCX XML extraction, TXT/DOCX/PDF UI contracts, and server startup were
  covered by the automated tests.
- VieNeu-TTS v3 Turbo generated a Vietnamese WAV on CPU (mono, 48 kHz) in a
  temporary cache; the audio was not added to the repository.
- Health checks confirm the app binds to `127.0.0.1` and cloud sync is off by
  default.

## Not yet claimed

- The GPU lock has not been exercised on a compatible NVIDIA machine.
- GitHub Actions and the GitHub Release still require pushing the commit/tag
  from an authenticated maintainer account.
- No tester, download, star, or adoption numbers are claimed until genuine
  users report reproducible feedback.
