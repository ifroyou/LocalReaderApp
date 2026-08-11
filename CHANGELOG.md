# Changelog

## Unreleased

- Replaced the AGPL/commercially licensed PyMuPDF dependency with pypdfium2 for PDF text extraction and rendering.
- Added pinned CPU/GPU requirement files plus fully resolved lock files used by the setup script.
- Disabled cloud synchronization by default in the local-first launcher.
- Removed the broad helper-file visibility watchdog from the public build.
- Added bilingual project documentation, MIT licensing, security guidance, tests, and contribution rules.
- Added a public-tree credential/generated-file check and a tag-triggered source-release workflow.
- Restored the public reader UI and vendored KaTeX assets required for a clone to serve the local app.
- Made the UI stop its cloud auto-sync loop when the server reports local-first cloud disabled.

## 0.1.0 - release candidate

The release notes are tracked in [RELEASE_NOTES_v0.1.0.md](RELEASE_NOTES_v0.1.0.md). The tag is created only after a clean Windows smoke test, CI checks, dependency notice review, and release archive verification.
