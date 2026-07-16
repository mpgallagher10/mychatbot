import io

from django.test import SimpleTestCase
from PIL import Image

from turns.services import images


def _make_jpeg(width: int, height: int) -> bytes:
    img = Image.new("RGB", (width, height), color=(120, 30, 200))
    out = io.BytesIO()
    img.save(out, format="JPEG")
    return out.getvalue()


class ImageProcessingTests(SimpleTestCase):
    def test_downsamples_large_image(self):
        original = _make_jpeg(4000, 3000)
        result = images.process_image(original, long_edge_px=1568, jpeg_quality=80)
        self.assertEqual(max(result.width, result.height), 1568)
        self.assertEqual(result.width, 1568)
        self.assertEqual(result.height, 1176)
        self.assertLess(result.bytes_downsampled, result.bytes_original)
        self.assertEqual(len(result.content_hash), 64)

    def test_does_not_upscale_small_image(self):
        original = _make_jpeg(800, 600)
        result = images.process_image(original, long_edge_px=1568)
        self.assertEqual((result.width, result.height), (800, 600))

    def test_content_hash_is_of_original_bytes(self):
        original = _make_jpeg(1000, 1000)
        self.assertEqual(
            images.process_image(original).content_hash,
            images.sha256_hex(original),
        )
