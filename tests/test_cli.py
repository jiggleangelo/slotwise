import contextlib
import io
import os
import tempfile
import unittest
from pathlib import Path

import cli


class CliTests(unittest.TestCase):
    def test_fake_output_is_private_and_overwrite_requires_force(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "record.json"
            stderr = io.StringIO()
            with contextlib.redirect_stderr(stderr):
                result = cli.main(["--scenario", "honest-3090", "--samples", "1", "--out", str(output)])
            self.assertEqual(result, 0)
            self.assertEqual(os.stat(output).st_mode & 0o777, 0o600)
            self.assertIn("serial", stderr.getvalue().lower())
            self.assertEqual(cli.main(["--samples", "1", "--out", str(output)]), 2)
            self.assertEqual(cli.main(["--samples", "1", "--out", str(output), "--force"]), 0)

    def test_oversized_baseline_is_rejected_cleanly(self):
        with tempfile.TemporaryDirectory() as directory:
            baseline = Path(directory) / "large.json"
            baseline.write_bytes(b" " * 32769)
            stderr = io.StringIO()
            with contextlib.redirect_stderr(stderr):
                result = cli.main(["--samples", "1", "--baseline", str(baseline), "--out", str(Path(directory) / "record.json")])
            self.assertEqual(result, 2)
            self.assertIn("32 KB", stderr.getvalue())
            self.assertNotIn("Traceback", stderr.getvalue())


if __name__ == "__main__":
    unittest.main()
