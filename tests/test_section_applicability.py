import json
import tempfile
import unittest
from pathlib import Path


class SectionApplicabilityTests(unittest.TestCase):
    def _bios_cache(self, payload):
        td = tempfile.TemporaryDirectory()
        self.addCleanup(td.cleanup)
        path = Path(td.name) / "bios-cache.json"
        path.write_text(json.dumps(payload), encoding="utf-8")
        return path

    def test_explicit_empty_spec_skips_even_when_bios_text_mentions_feature(self):
        from susi_gen import evaluate_section_applicability

        bios = self._bios_cache({
            "analysis_status": "DONE",
            "items": [{"analysis_status": "DONE_VISION_ANALYZE", "analysis_text": "Current sensor and Case Open"}],
        })

        current = evaluate_section_applicability("HWM.Current", {"currents": []}, bios)
        caseopen = evaluate_section_applicability("HWM.CaseOpen", {"caseopen": []}, bios)

        self.assertFalse(current["applicable"])
        self.assertEqual("SPEC_EXPLICITLY_DISABLED", current["reason_code"])
        self.assertFalse(caseopen["applicable"])
        self.assertEqual("SPEC_EXPLICITLY_DISABLED", caseopen["reason_code"])

    def test_unknown_spec_uses_positive_bios_evidence(self):
        from susi_gen import evaluate_section_applicability

        bios = self._bios_cache({
            "analysis_status": "DONE",
            "items": [{
                "analysis_status": "DONE_VISION_ANALYZE",
                "analysis_text": "PC Health Status: System Current 1.2 A; Chassis Intrusion Disabled",
            }],
        })

        current = evaluate_section_applicability("HWM.Current", {}, bios)
        caseopen = evaluate_section_applicability("HWM.CaseOpen", {}, bios)

        self.assertTrue(current["applicable"])
        self.assertEqual("BIOS_EVIDENCE_FOUND", current["reason_code"])
        self.assertTrue(caseopen["applicable"])
        self.assertEqual("BIOS_EVIDENCE_FOUND", caseopen["reason_code"])

    def test_unknown_spec_skips_when_completed_bios_has_no_feature_evidence(self):
        from susi_gen import evaluate_section_applicability

        bios = self._bios_cache({
            "analysis_status": "DONE",
            "items": [{
                "analysis_status": "DONE_VISION_ANALYZE",
                "analysis_text": "CPU Temperature; CPU FAN Speed; VBAT",
            }],
        })

        for section in ("HWM.Current", "HWM.CaseOpen"):
            with self.subTest(section=section):
                result = evaluate_section_applicability(section, {}, bios)
                self.assertFalse(result["applicable"])
                self.assertEqual("BIOS_EVIDENCE_NOT_FOUND", result["reason_code"])

    def test_explicit_enabled_spec_does_not_depend_on_bios_text(self):
        from susi_gen import evaluate_section_applicability

        bios = self._bios_cache({"analysis_status": "DONE", "items": []})

        current = evaluate_section_applicability("HWM.Current", {"currents": [{"name": "OEM0"}]}, bios)
        caseopen = evaluate_section_applicability("HWM.CaseOpen", {"caseopen": [{"name": "CO0"}]}, bios)

        self.assertTrue(current["applicable"])
        self.assertEqual("SPEC_EXPLICITLY_ENABLED", current["reason_code"])
        self.assertTrue(caseopen["applicable"])
        self.assertEqual("SPEC_EXPLICITLY_ENABLED", caseopen["reason_code"])

    def test_generator_skips_disabled_sections_even_when_db_has_rows(self):
        from susi_gen import _run_config_db_generate

        repo = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory() as td:
            case_dir = Path(td) / "MIO-TEST"
            case_dir.mkdir()
            request = case_dir / "MIO-TEST.json"
            request.write_text("{}", encoding="utf-8")
            (case_dir / "MIO-TEST-spec.json").write_text(
                json.dumps({"currents": [], "caseopen": []}), encoding="utf-8"
            )
            # Stale outputs from an earlier permissive run must be removed.
            for section in ("HWM.Current", "HWM.CaseOpen"):
                (case_dir / f"MIO-TEST_{section}.ini").write_text("stale", encoding="utf-8")

            _run_config_db_generate(
                project="MIO-TEST",
                in_json_path=request,
                out_ini_path=case_dir / "MIO-TEST-pre.ini",
                db_path=repo / "config_new.db",
                product_name="MIO",
                chip_name="EIO-300",
                probe_spec={"is_ec": True},
                probe_path=None,
                sections=["HWM.Current", "HWM.CaseOpen"],
            )

            full_ini = (case_dir / "MIO-TEST-pre.ini").read_text(encoding="utf-8")
            matrix = json.loads(
                (case_dir / "MIO-TEST-section-matrix.json").read_text(encoding="utf-8")
            )
            self.assertNotIn("[HWM.Current]", full_ini)
            self.assertNotIn("[HWM.CaseOpen]", full_ini)
            self.assertEqual(
                ["SKIPPED_NOT_APPLICABLE", "SKIPPED_NOT_APPLICABLE"],
                [entry["status"] for entry in matrix["sections"]],
            )
            self.assertFalse((case_dir / "MIO-TEST_HWM.Current.ini").exists())
            self.assertFalse((case_dir / "MIO-TEST_HWM.CaseOpen.ini").exists())

    def test_mio5854_current_and_caseopen_are_not_applicable(self):
        from susi_gen import evaluate_section_applicability

        case_dir = Path(__file__).resolve().parents[1] / "CASES" / "MIO-5854"
        spec = json.loads((case_dir / "MIO-5854-spec.json").read_text(encoding="utf-8"))
        bios = case_dir / "MIO-5854-bios-image-cache.json"

        self.assertFalse(evaluate_section_applicability("HWM.Current", spec, bios)["applicable"])
        self.assertFalse(evaluate_section_applicability("HWM.CaseOpen", spec, bios)["applicable"])


if __name__ == "__main__":
    unittest.main()
