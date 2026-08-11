# Third-party notices

This file records the direct runtime components used by LocalReaderApp. The exact transitive dependency set is resolved from the pinned requirement files during setup and must be reviewed before a binary distribution.

| Component | Use | License / notice |
| --- | --- | --- |
| `pypdfium2` | PDF text extraction and rendering | Apache-2.0 or BSD-3-Clause; PDFium and its bundled dependencies have additional notices that must accompany binary redistribution. |
| `pytesseract` | Python wrapper for Tesseract OCR | Apache-2.0 |
| Pillow | Image conversion and OCR preprocessing | MIT-CMU |
| `vieneu` | Vietnamese local TTS | Apache-2.0 |
| KaTeX 0.16.22 | Local math rendering in the reader UI | MIT; retain the upstream notice when redistributing the vendored files. |
| ONNX Runtime | CPU inference backend used by VieNeu | Retain the license metadata shipped with the installed distribution. |
| Tesseract OCR | External OCR executable and language data | Install and redistribute only under the terms of its own distribution and language-data licenses. |

LocalReaderApp's own code is MIT-licensed. This file is not a replacement for the license files shipped by each dependency. When packaging wheels or a compiled runtime, include the dependency license files and the pypdfium2/PDFium third-party notices with that package.
