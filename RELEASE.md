# Release checklist

This project ships a source archive and Windows batch setup. The `v0.1.0` tag must not be created until the maintainer has run the checks below on a clean Windows runtime.

## Before tagging

```powershell
python -m compileall -q .
python scripts/public_tree_check.py
python -m unittest discover -s tests -v
```

- Run **Setup Local Reader.bat** in a new runtime directory.
- Open the app and verify PDF text, scanned-PDF OCR, DOCX, TXT, and Vietnamese TTS.
- Check `/api/health`: `bind_host` is `127.0.0.1`, `cloud_enabled` is `false`, and the Tesseract/VieNeu paths are visible.
- Review `THIRD_PARTY_NOTICES.md`, `SECURITY.md`, and the final `git diff` for private data.
- Add user-visible notes to `CHANGELOG.md` and keep known limitations explicit.

## Tag and publish

After the checks pass, create and push the version tag. The release workflow validates the tag again, builds `LocalReaderApp-v0.1.0-source.zip`, writes a SHA-256 checksum, and attaches both files to the GitHub release.

The archive must contain the public UI (`index.html` and `vendor/katex/`) but no runtime state, credentials, generated audio, logs, backups, or private documents.

## After publishing

- Download the release archive as a fresh user and repeat the install smoke test. Confirm the setup script consumes the matching `requirements-*.lock.txt` file.
- Do not mark GPU/TTS as verified until a real NVIDIA machine has loaded the pinned GPU runtime and generated one Vietnamese sample.
- Record reproducible issues in GitHub Issues and link the release in the README or changelog when appropriate.
- Do not report stars, downloads, or tester counts unless they are genuine and verifiable.
