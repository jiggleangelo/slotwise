import json
import unittest
from cardcheck.compare import LIMITATIONS, compare
from cardcheck.listing import parse_listing
from cardcheck.record import build_record, canonical, record_html, record_json, validate_digest
from cardcheck.telemetry import FakeTelemetry
from cardcheck.version import __version__


class RecordTests(unittest.TestCase):
    def make(self):
        source = FakeTelemetry()
        claims = parse_listing({"model":"RTX 3090","vram_gb":"24","board_partner":"EVGA"})
        samples = [source.sample()]
        return build_record(claims, source.identity(), samples, compare(claims, source.identity(), samples), created_utc="2026-10-24T00:00:00+00:00")

    def test_canonical_and_digest(self):
        first, second = self.make(), self.make()
        self.assertEqual(canonical(first.data), canonical(second.data))
        self.assertTrue(validate_digest(first.data))
        first.data["claims"]["notes"] = "changed"
        self.assertFalse(validate_digest(first.data))

    def test_limitations_always_present(self):
        self.assertEqual(self.make().data["limitations"], LIMITATIONS)

    def test_f2_record_import_and_exports_use_defined_version(self):
        self.assertEqual(__version__, "0.1.0")
        record = self.make()
        self.assertEqual(record.data["tool_version"], __version__)
        exported = json.loads(record_json(record))
        self.assertEqual(exported["tool_version"], "0.1.0")
        self.assertTrue(validate_digest(exported))
        self.assertIn("Tool: 0.1.0", record_html(record))


if __name__ == "__main__":
    unittest.main()
