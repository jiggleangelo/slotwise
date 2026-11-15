import contextlib
import io
import unittest
from cardcheck.vault import FakeVault


class VaultTests(unittest.TestCase):
    def test_write_only_and_silent(self):
        vault = FakeVault()
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            token = vault.store("email", "made-up@example.invalid")
        self.assertTrue(token.startswith("vault-"))
        self.assertFalse(hasattr(vault, "read"))
        self.assertEqual(output.getvalue(), "")
        self.assertNotIn("made-up", repr(vars(vault)))


if __name__ == "__main__":
    unittest.main()
