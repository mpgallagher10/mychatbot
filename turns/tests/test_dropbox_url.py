from django.test import SimpleTestCase

from turns.services.dropbox_client import is_shared_url, normalize_dropbox_shared_url


class DropboxUrlTests(SimpleTestCase):
    def test_is_shared_url(self):
        self.assertTrue(
            is_shared_url("https://www.dropbox.com/scl/fo/abc/XYZ?rlkey=k1&st=x&dl=0")
        )
        self.assertFalse(is_shared_url("/Turns/P-1/2026-07-16"))
        self.assertFalse(is_shared_url("https://drive.google.com/drive/folders/abc"))

    def test_normalize_strips_volatile_params_keeps_rlkey(self):
        url = "https://www.dropbox.com/scl/fo/526x/ALCK?rlkey=7fff&st=vjdifljn&dl=0"
        self.assertEqual(
            normalize_dropbox_shared_url(url),
            "https://www.dropbox.com/scl/fo/526x/ALCK?rlkey=7fff",
        )

    def test_normalize_is_idempotent(self):
        once = normalize_dropbox_shared_url(
            "https://www.dropbox.com/scl/fo/526x/ALCK?rlkey=7fff&st=a&dl=0"
        )
        self.assertEqual(normalize_dropbox_shared_url(once), once)
