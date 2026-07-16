from django.test import SimpleTestCase

from turns.services.photo_source import SourceKind, classify_source


class ClassifySourceTests(SimpleTestCase):
    def test_dropbox_path(self):
        self.assertEqual(
            classify_source("/Turns/P-1/2026-07-16"), SourceKind.DROPBOX_PATH
        )

    def test_dropbox_url(self):
        self.assertEqual(
            classify_source("https://www.dropbox.com/scl/fo/abc/xyz"),
            SourceKind.DROPBOX_URL,
        )

    def test_drive_url(self):
        self.assertEqual(
            classify_source("https://drive.google.com/drive/folders/abc"),
            SourceKind.DRIVE_URL,
        )

    def test_unknown(self):
        self.assertEqual(classify_source(""), SourceKind.UNKNOWN)
        self.assertEqual(classify_source("ftp://x/y"), SourceKind.UNKNOWN)
