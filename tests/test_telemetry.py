import ast
import inspect
import unittest
from unittest.mock import MagicMock, patch
import cardcheck.telemetry as telemetry


class TelemetryTests(unittest.TestCase):
    def test_fake_is_stable(self):
        source = telemetry.FakeTelemetry("honest-3090")
        self.assertEqual(source.identity(), source.identity())
        self.assertEqual(source.sample(), source.sample())

    def test_missing_dll_is_clean(self):
        with patch.object(telemetry.ctypes, "WinDLL", side_effect=OSError(), create=True):
            with self.assertRaises(telemetry.TelemetryUnavailable):
                telemetry.NvmlTelemetry()

    def test_every_nvml_symbol_literal_is_allowlisted(self):
        tree = ast.parse(inspect.getsource(telemetry))
        literals = {
            node.value for node in ast.walk(tree)
            if isinstance(node, ast.Constant) and isinstance(node.value, str) and node.value.startswith("nvml")
        }
        self.assertTrue(literals)
        self.assertLessEqual(literals, telemetry.NVML_ALLOWLIST)
        self.assertFalse(hasattr(telemetry.NvmlTelemetry, "dll"))

    def test_shutdown_is_called(self):
        dll = MagicMock()
        dll.nvmlInit_v2.return_value = 0
        dll.nvmlShutdown.return_value = 0
        with patch.object(telemetry.ctypes, "WinDLL", return_value=dll, create=True):
            source = telemetry.NvmlTelemetry()
            self.assertFalse(hasattr(source, "dll"))
            source.close()
        dll.nvmlShutdown.assert_called_once_with()


if __name__ == "__main__":
    unittest.main()
