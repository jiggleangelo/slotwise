import unittest
from dataclasses import replace
from cardcheck.baseline import parse_baseline
from cardcheck.compare import compare, summary
from cardcheck.listing import parse_listing
from cardcheck.observe import observe
from cardcheck.reference import REFERENCE
from cardcheck.telemetry import FakeTelemetry


class CompareTests(unittest.TestCase):
    def run_scenario(self, name):
        values = {"honest-3090":("RTX 3090","24","EVGA","DEMO-0001"),"relabelled-4090":("RTX 4090","24","NVIDIA","DEMO-4090"),"worn-3090":("RTX 3090","24","EVGA","DEMO-9999")}[name]
        claims = parse_listing({"model":values[0],"vram_gb":values[1],"board_partner":values[2],"serial":values[3]})
        source = FakeTelemetry(name)
        samples = observe(source, 3, lambda _: None)
        baseline = parse_baseline('{"schema":"cardcheck.baseline/1","serial":"DEMO-0001"}')
        return compare(claims, source.identity(), samples, baseline)

    def test_scenarios(self):
        bad = self.run_scenario("relabelled-4090")
        self.assertIn("R2", {f.rule_id for f in bad if f.severity == "mismatch"})
        self.assertIn("R3", {f.rule_id for f in bad if f.severity == "mismatch"})
        worn = self.run_scenario("worn-3090")
        self.assertTrue({"R6", "R7"} <= {f.rule_id for f in worn if f.severity == "warning"})
        self.assertIn("R10", {f.rule_id for f in worn if f.severity == "mismatch"})

    def test_verified_reference_enables_r1(self):
        source = FakeTelemetry("relabelled-4090")
        claims = parse_listing({"model":"RTX 4090","vram_gb":"24","board_partner":"NVIDIA"})
        refs = {key: dict(value) for key, value in REFERENCE.items()}
        refs["RTX 4090"]["verified"] = True
        findings = compare(claims, source.identity(), [source.sample()], ref_table=refs)
        self.assertIn("R1", {f.rule_id for f in findings if f.severity == "mismatch"})

    def test_no_prohibited_claims(self):
        text = " ".join(f.explanation.lower() for f in self.run_scenario("honest-3090"))
        cleaned = text.replace("unverified", "")
        for word in ("verified", "certified", "pass"):
            self.assertNotIn(word, cleaned)


if __name__ == "__main__":
    unittest.main()
