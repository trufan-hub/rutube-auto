import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from io import BytesIO
from PIL import Image
import logo_cache as cache


class LogoTests(unittest.TestCase):
    def test_migration_outage_and_integrity(self):
        ident = "rtv." + "a" * 32
        external = "https://iptvx.one/picons/test.png"
        output = BytesIO()
        Image.new("RGBA", (24, 24), (255, 0, 0, 255)).save(output, "PNG")
        with tempfile.TemporaryDirectory() as directory:
            with patch.object(cache, "ASSETS", Path(directory)):
                with patch.object(cache, "download_logo", side_effect=OSError):
                    self.assertEqual(cache.mirror_logos({ident: external}, {}), {ident: external})
                with patch.object(cache, "download_logo", return_value=output.getvalue()):
                    result = cache.mirror_logos({ident: external}, {})
                self.assertEqual(result[ident], cache.local_url(ident))
                cache.validate_local_registry(result)
                with patch.object(cache, "download_logo", side_effect=AssertionError):
                    self.assertEqual(cache.mirror_logos(result, {}), result)
                cache.asset_path(ident).unlink()
                with self.assertRaises(ValueError):
                    cache.validate_local_registry(result)
                with self.assertRaises(ValueError):
                    cache.mirror_logos(result, {})


if __name__ == "__main__":
    unittest.main()
