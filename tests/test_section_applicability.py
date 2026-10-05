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

    def test_bios_presence_enables_hwm_even_when_spec_is_explicitly_empty(self):
        from susi_gen import evaluate_section_applicability

        bios = self._bios_cache({
            "analysis_status": "DONE",
            "items": [{
                "analysis_status": "DONE_VISION_ANALYZE",
                "analysis_text": "CURRENT_VALUE: System Current=1.2A; CASEOPEN_VALUE: Case Open=No",
                "current_value_hints": [{"label": "System Current", "value": 1.2, "unit": "A"}],
                "caseopen_hints": [{"label": "Case Open", "state": "No"}],
            }],
        })

        current = evaluate_section_applicability("HWM.Current", {"currents": []}, bios)
        caseopen = evaluate_section_applicability("HWM.CaseOpen", {"caseopen": []}, bios)

        self.assertTrue(current["applicable"])
        self.assertEqual("BIOS_EVIDENCE_FOUND", current["reason_code"])
        self.assertTrue(caseopen["applicable"])
        self.assertEqual("BIOS_EVIDENCE_FOUND", caseopen["reason_code"])

    def test_live_current_and_caseopen_readings_are_parsed(self):
        from susi_gen import _extract_caseopen_hints, _extract_current_value_hints

        self.assertEqual(
            _extract_current_value_hints("PC Health Status: System Current 1.2 A"),
            [{"label": "System Current", "value": 1.2, "unit": "A"}],
        )
        self.assertEqual(
            _extract_current_value_hints("CURRENT_VALUE: CPU Current=850mA"),
            [{"label": "CPU Current", "value": 850.0, "unit": "mA"}],
        )
        self.assertEqual(_extract_caseopen_hints("CASEOPEN_VALUE: Chassis Intrusion=Open"),
                         [{"label": "Chassis Intrusion", "state": "Open"}])
        # Settings and negative sentences are not live readings.
        self.assertEqual(_extract_caseopen_hints("Case Open Detection"), [])
        self.assertEqual(_extract_caseopen_hints("Chassis Intrusion [Disabled]"), [])
        self.assertEqual(_extract_caseopen_hints("No Case Open / Chassis Intrusion items are visible."), [])

    def test_only_hardware_monitor_page_counts_as_hwm_evidence(self):
        # MIO-5854 shape: bios1 is the Hardware Monitor page; the others are
        # CPU/iManager/main pages whose prose mentions sensors negatively.
        from susi_gen import evaluate_section_applicability

        bios = self._bios_cache({
            "items": [
                {"filename": "bios1.jpg", "analysis_status": "DONE_VISION_ANALYZE",
                 "analysis_text": "VBAT=3.01V; CPU Temperature=53C; CPU FAN Speed=0RPM. No current or chassis/case-open live item.",
                 "voltage_value_hints": [{"label": "VBAT", "value": 3.01, "unit": "V"}],
                 "temperature_value_hints": [{"label": "CPU TEMPERATURE", "value": 53.0, "unit": "C"}],
                 "fan_value_hints": [{"label": "CPU FAN SPEED", "value": 0.0, "unit": "RPM"}]},
                {"filename": "bios2.jpg", "analysis_status": "DONE_VISION_ANALYZE",
                 "analysis_text": "No visible voltage rails, temperature, fan, current/ampere monitors, or Case Open / Chassis Intrusion items.",
                 "voltage_label_hints": ["+12V"], "voltage_value_hints": [], "temperature_value_hints": [], "fan_value_hints": []},
                {"filename": "bios3.jpg", "analysis_status": "DONE_VISION_ANALYZE",
                 "analysis_text": "Case Open Detection\n\nNo visible live voltage, temperature, fan, or current readings.",
                 "voltage_value_hints": [], "temperature_value_hints": [], "fan_value_hints": []},
            ],
        })

        for section in ("HWM.Voltage", "HWM.Temperature", "HWM.Fan", "HWM.Fan.Control"):
            self.assertTrue(evaluate_section_applicability(section, {}, bios)["applicable"], section)
        for section in ("HWM.Current", "HWM.CaseOpen"):
            self.assertFalse(evaluate_section_applicability(section, {}, bios)["applicable"], section)

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

    def test_bios_absence_disables_hwm_even_when_spec_is_explicitly_enabled(self):
        from susi_gen import evaluate_section_applicability

        bios = self._bios_cache({"analysis_status": "DONE", "items": []})

        current = evaluate_section_applicability("HWM.Current", {"currents": [{"name": "OEM0"}]}, bios)
        caseopen = evaluate_section_applicability("HWM.CaseOpen", {"caseopen": [{"name": "CO0"}]}, bios)

        self.assertFalse(current["applicable"])
        self.assertEqual("BIOS_EVIDENCE_NOT_FOUND", current["reason_code"])
        self.assertFalse(caseopen["applicable"])
        self.assertEqual("BIOS_EVIDENCE_NOT_FOUND", caseopen["reason_code"])

    def test_bios_gate_covers_all_hwm_sections_but_not_non_hwm_sections(self):
        from susi_gen import evaluate_section_applicability

        bios = self._bios_cache({
            "analysis_status": "DONE",
            "items": [{
                "analysis_status": "DONE_VISION_ANALYZE",
                "voltage_label_hints": ["+12V"],
                "temperature_value_hints": [{"label": "CPU Temperature", "value": 42, "unit": "C"}],
                "fan_value_hints": [{"label": "CPU FAN", "value": 1800, "unit": "RPM"}],
                "current_value_hints": [{"label": "System Current", "value": 1.2, "unit": "A"}],
                "caseopen_hints": ["Chassis Intrusion"],
            }],
        })

        for section in (
            "HWM.Voltage",
            "HWM.Temperature",
            "HWM.Fan",
            "HWM.Fan.Control",
            "HWM.Current",
            "HWM.CaseOpen",
        ):
            with self.subTest(section=section):
                result = evaluate_section_applicability(section, {}, bios)
                self.assertTrue(result["applicable"])
                self.assertEqual("BIOS_EVIDENCE_FOUND", result["reason_code"])

        non_hwm = evaluate_section_applicability("SMBus", {}, self._bios_cache({"items": []}))
        self.assertTrue(non_hwm["applicable"])
        self.assertEqual("SECTION_NOT_BIOS_GATED", non_hwm["reason_code"])

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
