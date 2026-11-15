import unittest
from cardcheck.baseline import BaselineError, parse_baseline
from cardcheck.compare import compare
from cardcheck.listing import parse_listing
from cardcheck.observe import observe
from cardcheck.record import build_record, record_html
from cardcheck.telemetry import FakeTelemetry


class BaselineTests(unittest.TestCase):
    def test_rejects_size_duplicate_depth_and_type(self):
        cases = [b" " * 32769, '{"schema":"cardcheck.baseline/1","serial":"a","serial":"b"}', '{"schema":"cardcheck.baseline/1","x":{"a":{"b":{"c":1}}}}', '{"schema":"cardcheck.baseline/1","vram_total_mb":"large"}']
        for raw in cases:
            with self.subTest(raw=str(raw)[:30]), self.assertRaises(BaselineError):
                parse_baseline(raw)

    def test_unknown_key_and_escaping(self):
        baseline = parse_baseline('{"schema":"cardcheck.baseline/1","vbios_version":"<script>x</script>","future":1}')
        self.assertEqual(baseline.ignored_keys, ["future"])
        source = FakeTelemetry()
        claims = parse_listing({"model":"RTX 3090","vram_gb":"24","board_partner":"EVGA"})
        samples = observe(source, 1, lambda _: None)
        record = build_record(claims, source.identity(), samples, compare(claims, source.identity(), samples, baseline), baseline)
        rendered = record_html(record)
        self.assertNotIn("<script>", rendered)
        self.assertIn("&lt;script&gt;", rendered)


if __name__ == "__main__":
    unittest.main()
