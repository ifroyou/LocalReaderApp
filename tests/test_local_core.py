import os
import io
import json
import threading
import unittest
import urllib.request
import zipfile
from unittest.mock import patch

os.environ.setdefault("LOCAL_READER_CLOUD_ENABLED", "0")

import reader_server
import open_reader


class FakeTextPage:
    def __init__(self, text):
        self.text = text

    def get_text_range(self):
        return self.text


class FakeBitmap:
    def to_pil(self):
        from PIL import Image

        return Image.new("RGB", (160, 100), "white")


class FakePage:
    def __init__(self, text):
        self.text = text

    def get_textpage(self):
        return FakeTextPage(self.text)

    def get_size(self):
        return (160.0, 100.0)

    def render(self, scale):
        self.scale = scale
        return FakeBitmap()


class FakeDocument:
    def __init__(self, pdf_bytes):
        self.pages = [
            FakePage("Xin chao LocalReaderApp. " * 12),
            FakePage("Trang hai."),
        ]
        self.closed = False

    def __len__(self):
        return len(self.pages)

    def __getitem__(self, index):
        return self.pages[index]

    def close(self):
        self.closed = True


class LocalCoreTests(unittest.TestCase):
    def test_pdf_text_layer_uses_one_based_page_labels(self):
        text = reader_server.text_layer_from_pdf(FakeDocument(b"pdf"))
        self.assertIn("[Page 1]", text)
        self.assertIn("[Page 2]", text)

    def test_pdf_extraction_returns_text_layer_result(self):
        with patch.object(reader_server.pdfium, "PdfDocument", FakeDocument):
            result = reader_server.extract_pdf_bytes(b"pdf")

        self.assertEqual(result["method"], "text_layer")
        self.assertEqual(result["pages"], 2)
        self.assertGreater(result["char_count"], 120)

    def test_pdf_page_render_returns_grayscale_image_for_ocr(self):
        image = reader_server.render_page_for_ocr(FakePage(""), dpi=220)
        self.assertEqual(image.mode, "L")
        self.assertEqual(image.size, (160, 100))

    def test_pdf_ocr_path_uses_page_limit_and_one_based_labels(self):
        class ExistingPath:
            def exists(self):
                return True

        with patch.object(reader_server, "available_ocr_lang", return_value="vie"), \
             patch.object(reader_server, "render_page_for_ocr", return_value=object()), \
             patch.object(reader_server.pytesseract, "image_to_string", return_value="Xin chao OCR"), \
             patch.object(reader_server, "TESSERACT_EXE", ExistingPath()), \
             patch.object(reader_server, "TESSDATA_DIR", ExistingPath()):
                text, processed, lang = reader_server.ocr_pdf(FakeDocument(b"pdf"), max_pages=1)

        self.assertEqual(processed, 1)
        self.assertEqual(lang, "vie")
        self.assertEqual(text, "[Page 1]\nXin chao OCR")

    def test_docx_xml_extracts_paragraphs_tabs_breaks_and_math(self):
        document_xml = b'''<?xml version="1.0" encoding="UTF-8"?>
        <w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"
                    xmlns:m="http://schemas.openxmlformats.org/officeDocument/2006/math">
          <w:body>
            <w:p><w:r><w:t>Xin chao DOCX</w:t></w:r><w:r><w:tab/><w:t>LocalReader</w:t><w:br/><w:t>App</w:t></w:r></w:p>
            <w:p><m:oMath><m:r><m:t>x</m:t></m:r><m:r><m:t>2</m:t></m:r></m:oMath></w:p>
          </w:body>
        </w:document>'''
        payload = io.BytesIO()
        with zipfile.ZipFile(payload, "w", zipfile.ZIP_DEFLATED) as archive:
            archive.writestr("word/document.xml", document_xml)

        result = reader_server.extract_docx_bytes(payload.getvalue())

        self.assertEqual(result["method"], "docx_xml")
        self.assertEqual(result["math_count"], 1)
        self.assertIn("Xin chao DOCX", result["text"])
        self.assertIn("LocalReader", result["text"])
        self.assertIn("\\(", result["text"])

    def test_public_ui_contract_covers_txt_docx_pdf_and_katex(self):
        root = os.path.dirname(os.path.dirname(__file__))
        with open(os.path.join(root, "index.html"), encoding="utf-8") as handle:
            html = handle.read()
        self.assertIn("accept=\".txt,.md,.csv,.log,.html,.htm,.pdf,.docx\"", html)
        self.assertIn("await file.text()", html)
        self.assertIn("/api/extract_pdf", html)
        self.assertIn("/api/extract_docx", html)
        self.assertIn("cloudStatus.enabled === false", html)
        self.assertTrue(os.path.isfile(os.path.join(root, "vendor", "katex", "katex.min.js")))

    def test_server_startup_serves_ui_and_local_health(self):
        server = reader_server.LocalReaderHTTPServer(("127.0.0.1", 0), reader_server.Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        base = f"http://127.0.0.1:{server.server_port}"
        try:
            headers = {"Host": f"127.0.0.1:{reader_server.PORT}"}
            with urllib.request.urlopen(urllib.request.Request(base + "/", headers=headers), timeout=3) as response:
                html_status = response.status
                html = response.read()
            with urllib.request.urlopen(urllib.request.Request(base + "/api/health", headers=headers), timeout=3) as response:
                health_status = response.status
                health = json.load(response)
        finally:
            server.shutdown()
            server.server_close()

        self.assertEqual(html_status, 200)
        self.assertIn(b"Local Reader", html)
        self.assertEqual(health_status, 200)
        self.assertEqual(health["bind_host"], "127.0.0.1")
        self.assertFalse(health["cloud_enabled"])
        self.assertEqual(open_reader.APP_BUILD_ID, reader_server.APP_BUILD_ID)

    def test_cloud_is_disabled_by_default_for_local_build(self):
        self.assertEqual(reader_server.BIND_HOST, "127.0.0.1")
        self.assertEqual(reader_server.preferred_cloud_provider(), "")
        status = reader_server.cloud_status_payload()
        self.assertFalse(status["enabled"])
        self.assertFalse(status["configured"])
        self.assertEqual(status["limit_bytes"], 0)

        mutations = (
            reader_server.sync_project_to_cloud("doc-1"),
            reader_server.sync_all_projects_to_cloud(),
            reader_server.sync_everything_to_r2(),
            reader_server.rebuild_cloud_library(),
            reader_server.delete_projects_from_cloud(["doc-1"]),
        )
        self.assertTrue(all(item["skipped"] for item in mutations))


if __name__ == "__main__":
    unittest.main()
