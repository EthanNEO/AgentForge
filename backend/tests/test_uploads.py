import base64
import tempfile
import unittest
from pathlib import Path

from app import config
from app.uploads import create_upload, materialize_uploads, resolve_uploads


class UploadTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.original_uploads = config.UPLOADS_DIR
        config.UPLOADS_DIR = Path(self.tmp.name) / "uploads"

    def tearDown(self):
        config.UPLOADS_DIR = self.original_uploads
        self.tmp.cleanup()

    @staticmethod
    def encoded(value: bytes) -> str:
        return base64.b64encode(value).decode("ascii")

    def test_upload_persists_safe_text_and_image_metadata(self):
        payload = create_upload({"files": [
            {"name": "notes.txt", "type": "text/plain", "data": self.encoded(b"hello")},
            {"name": "screen.png", "type": "image/png", "data": self.encoded(b"\x89PNG\r\n")},
        ]})

        self.assertEqual(len(payload["files"]), 2)
        self.assertEqual(payload["files"][1]["kind"], "image")
        resolved = resolve_uploads([payload["upload_id"]])
        self.assertEqual([item["name"] for item in resolved], ["notes.txt", "screen.png"])

    def test_upload_rejects_traversal_invalid_base64_and_unsupported_type(self):
        cases = [
            {"name": "../secret.txt", "type": "text/plain", "data": self.encoded(b"x")},
            {"name": "bad.txt", "type": "text/plain", "data": "%%%"},
            {"name": "run.exe", "type": "application/octet-stream", "data": self.encoded(b"x")},
        ]
        for file in cases:
            with self.subTest(file=file["name"]), self.assertRaises(ValueError):
                create_upload({"files": [file]})

    def test_materialize_copies_only_server_issued_uploads(self):
        payload = create_upload({"files": [
            {"name": "brief.md", "type": "text/markdown", "data": self.encoded(b"# brief")},
        ]})
        target = Path(self.tmp.name) / "run" / "inputs"

        items = materialize_uploads([payload["upload_id"]], target)

        self.assertEqual((target / "brief.md").read_text(encoding="utf-8"), "# brief")
        self.assertEqual(items[0]["relative_path"], "inputs/brief.md")
        with self.assertRaises(KeyError):
            materialize_uploads(["not-issued"], target)


if __name__ == "__main__":
    unittest.main()
