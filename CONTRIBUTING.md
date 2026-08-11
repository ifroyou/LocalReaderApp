# Contributing

## Before opening a pull request

- Keep document data, audio, caches, local runtime folders, and credentials out of commits.
- Keep the default workflow local-only and bound to localhost.
- Update the relevant README or changelog entry for user-visible behavior.
- Run the checks below from the repository root:

```powershell
python -m compileall -q .
python -m unittest discover -s tests -v
```

## Pull requests

Describe the user-visible change, the files or formats tested, and any platform-specific limitation. Small, focused pull requests are easier to review. A maintainer may request a regression test, a dependency/license note, or a reproducible sample that contains no private data.

## Release discipline

Releases use a version tag and a changelog entry. Follow [RELEASE.md](RELEASE.md); do not change pinned dependencies or publish an archive without a clean-install smoke test and updated third-party notices.
