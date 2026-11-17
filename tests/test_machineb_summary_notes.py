import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from test_machineb_full_validation_pipeline import make_contract  # noqa: E402


GPIO_OK = {
    "metrics": {
        "modes": {"single": "success", "multiple": "success"},
        "banks": {"Bank0": {}, "Bank1": {}},
        "single": {"GPIO00": {"ok": True}, "GPIO01": {"ok": True}, "GPIO37": {"ok": True}},
    }
}
GPIO_FAIL = {
    "metrics": {
        "modes": {"single": "fail", "multiple": "success"},
        "banks": {"Bank0": {}},
        "single": {"GPIO00": {"ok": True}, "GPIO05": {"ok": False}},
        "single_failed_pins": ["GPIO05 (group 3, bit 2)"],
    }
}
THERMAL_OK = {
    "metrics": {
        "events": {
            "TPCH0": {"none": "success", "shutdown": "success", "throttle": "success", "poweroff": "unsupported"},
            "TPCH1": {"none": "success", "shutdown": "success", "throttle": "success", "poweroff": "unsupported"},
        }
    }
}
THERMAL_FAIL = {
    "metrics": {
        "events": {
            "TPCH0": {"none": "success", "shutdown": "success", "throttle": "success", "poweroff": "unsupported"},
            "TPCH1": {"none": "success", "shutdown": "fail", "throttle": "success", "poweroff": "unsupported"},
        }
    }
}


class DetailNoteTests(unittest.TestCase):
    def note(self, section, report):
        from run_machineB_full_validation import _section_detail_note

        return _section_detail_note(section, report)

    def test_gpio_note_lists_both_modes(self):
        self.assertEqual(
            self.note("GPIO", GPIO_OK),
            "modes: single success (3/3 pins ok), multiple success (2 banks)",
        )

    def test_gpio_note_names_failed_pins(self):
        self.assertEqual(
            self.note("GPIO", GPIO_FAIL),
            "modes: single fail (1/2 pins ok; failed: GPIO05 (group 3, bit 2)), multiple success (1 bank)",
        )

    def test_thermal_note_summarises_events_over_all_zones(self):
        self.assertEqual(
            self.note("ThermalProtect", THERMAL_OK),
            "events (2 zones): none ok, shutdown ok, throttle ok, poweroff unsupported",
        )

    def test_thermal_note_names_the_failed_zone_and_event(self):
        self.assertEqual(
            self.note("ThermalProtect", THERMAL_FAIL),
            "events (2 zones): none ok, shutdown TPCH1 fail, throttle ok, poweroff unsupported",
        )

    def test_no_note_for_other_sections_or_old_reports(self):
        self.assertEqual(self.note("WDT", GPIO_OK), "")
        self.assertEqual(self.note("GPIO", {"metrics": {}}), "")
        self.assertEqual(self.note("ThermalProtect", {}), "")
        self.assertEqual(self.note("GPIO", {"metrics": {"modes": "bad"}}), "")


class SummaryTextTests(unittest.TestCase):
    def test_note_is_an_indented_line_under_the_unchanged_section_line(self):
        from run_machineB_full_validation import write_validation_summary

        with tempfile.TemporaryDirectory() as td:
            contract = make_contract(td)
            write_validation_summary(
                contract,
                runtime_ini={"status": "PASS"},
                reload_result={"status": "PASS"},
                section_results=[
                    {"section": "GPIO", "execution_status": "COMPLETED", "verdict": "PASS",
                     "reason": "GPIO low/high readback and restore passed.",
                     "detail_note": "modes: single success (3/3 pins ok), multiple success (2 banks)"},
                    {"section": "WDT", "execution_status": "COMPLETED", "verdict": "PASS", "reason": "ok"},
                ],
                rollback={"status": "PASS"},
                errors=[],
                warnings=[],
            )
            lines = contract.outputs.summary_text.read_text(encoding="utf-8").splitlines()
        gpio_index = next(i for i, line in enumerate(lines) if "[PASS]  GPIO" in line)
        self.assertTrue(lines[gpio_index].endswith("GPIO low/high readback and restore passed."))
        note = lines[gpio_index + 1]
        self.assertTrue(note.startswith(" " * 10))
        self.assertIn("modes: single success (3/3 pins ok), multiple success (2 banks)", note)
        self.assertIn("[PASS]  WDT", lines[gpio_index + 2])


if __name__ == "__main__":
    unittest.main()


class FallbackDetailNoteTests(unittest.TestCase):
    """After a fallback converges, the note must describe the winning report, not the baseline."""

    def _run(self, winner_payload):
        import json
        from types import SimpleNamespace

        from run_machineB_full_validation import finalize_summary_after_fallback, write_validation_summary
        from test_machineb_full_validation_pipeline import FallbackFinalSummaryTests

        helper = FallbackFinalSummaryTests()
        with tempfile.TemporaryDirectory() as td:
            contract = make_contract(td)
            contract.inputs = SimpleNamespace(full_ini=Path(td) / "BOARD-pre.ini")
            write_validation_summary(
                contract,
                runtime_ini={"status": "PASS"},
                reload_result={"status": "PASS"},
                section_results=[{
                    "section": "GPIO",
                    "execution_status": "COMPLETED",
                    "verdict": "FAIL",
                    "result": "FAIL_API",
                    "reason": "GPIO capability or initial read failed.",
                    "detail_note": "modes: single not_run, multiple not_run (1 bank)",
                    "local_report_path": str(contract.outputs.reports_dir / "gpio_1.json"),
                }],
                rollback={"status": "PASS"},
                errors=[],
                warnings=[],
            )
            report_path = (
                contract.outputs.run_root / "fallback" / "gpio" / "attempt-002" / "reports" / "gpio_2.json"
            )
            report_path.parent.mkdir(parents=True, exist_ok=True)
            report_path.write_text(json.dumps(winner_payload), encoding="utf-8-sig")
            attempts = [{"index": 2, "success": True, "route_value": "0x2E", "changed_keys": [],
                         "report_path": str(report_path)}]
            summary = finalize_summary_after_fallback(
                contract, helper._convergence(contract, status="CONVERGED", attempts=attempts)
            )
            text = contract.outputs.summary_text.read_text(encoding="utf-8")
        gpio = next(item for item in summary["sections"] if item["section"] == "GPIO")
        return gpio, text

    def test_note_comes_from_the_winning_report(self):
        payload = {"category": "GPIO", "result": "PASS", "reason": "ok", **GPIO_OK}
        gpio, text = self._run(payload)
        self.assertEqual(
            gpio["detail_note"], "modes: single success (3/3 pins ok), multiple success (2 banks)"
        )
        self.assertNotIn("not_run", text)

    def test_stale_baseline_note_is_dropped_when_winner_has_no_metrics(self):
        gpio, text = self._run({"category": "GPIO", "result": "PASS", "reason": "ok"})
        self.assertEqual(gpio["detail_note"], "")
        self.assertNotIn("not_run", text)
