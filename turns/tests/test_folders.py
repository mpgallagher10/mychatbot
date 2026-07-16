from datetime import date

from django.test import SimpleTestCase

from turns.services.folders import (
    FolderConventionError,
    build_folder_path,
    parse_folder_path,
)


class FolderConventionTests(SimpleTestCase):
    def test_build(self):
        self.assertEqual(
            build_folder_path("P-123", date(2026, 7, 16)),
            "/Turns/P-123/2026-07-16",
        )

    def test_parse_valid_with_and_without_trailing_slash(self):
        for path in ("/Turns/P-123/2026-07-16", "/Turns/P-123/2026-07-16/"):
            ref = parse_folder_path(path)
            self.assertEqual(ref.property_id, "P-123")
            self.assertEqual(ref.walk_date, date(2026, 7, 16))
            self.assertEqual(ref.normalized_path, "/Turns/P-123/2026-07-16")

    def test_parse_missing_leading_slash_is_tolerated(self):
        ref = parse_folder_path("Turns/P-9/2026-01-02/")
        self.assertEqual(ref.property_id, "P-9")

    def test_parse_rejects_bad_shape(self):
        for bad in ("", "/Photos/P-1/2026-07-16", "/Turns/P-1/2026-7-6", "/Turns/P-1"):
            with self.assertRaises(FolderConventionError):
                parse_folder_path(bad)

    def test_parse_rejects_impossible_date(self):
        with self.assertRaises(FolderConventionError):
            parse_folder_path("/Turns/P-1/2026-13-40")
