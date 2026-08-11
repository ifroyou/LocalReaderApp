# LocalReaderApp v0.1.0 — tester checklist

Use only synthetic or non-sensitive sample files. Never attach private
documents, credentials, customer data, unredacted logs, or personal audio.

## Trước khi thử / Before testing

- Windows 10/11 with Python 3.11 or 3.12.
- Install Tesseract OCR and `vie.traineddata`.
- Run `Setup Local Reader.bat`, then `Local Reader.bat`.

## Test cases

1. Open `http://127.0.0.1:8765`; confirm the app loads and `/api/health`
   reports `bind_host=127.0.0.1` and `cloud_enabled=false`.
2. Open a text-layer PDF and confirm Vietnamese text is readable.
3. Open an image-only/scanned PDF and confirm Vietnamese OCR produces text.
4. Open a DOCX with Vietnamese paragraphs and confirm text extraction.
5. Open a UTF-8 TXT/Markdown file and confirm Vietnamese characters display.
6. Use local Vietnamese read-aloud and confirm a WAV/audio result is generated.
7. Close and reopen the app; confirm no cloud account is requested.

## Feedback template / Mẫu phản hồi

```text
Environment (Windows/Python/CPU-GPU/Tesseract):
Test case and synthetic sample:
Steps:
Expected:
Actual:
Severity: blocker / major / minor / suggestion
Privacy check: synthetic or redacted data only
Logs or screenshot: attach only after removing paths, tokens, names, and text
```

Please report results in [Issue #1](https://github.com/ifroyou/LocalReaderApp/issues/1)
or open a separate reproducible bug report.
