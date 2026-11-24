import importlib.util
import re
import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
RUNNER_PATH = ROOT / "targetB_task" / "machineB_validation" / "run_wdt_validation.ps1"


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


full = _load("machineb_full_wdt_sci", ROOT / "run_machineB_full_validation.py")


class WdtSciRunnerTests(unittest.TestCase):
    def setUp(self):
        self.text = RUNNER_PATH.read_text(encoding="utf-8-sig")
        self.code = re.sub(r"#.*", "", self.text)

    def test_sci_is_gated_by_the_support_flag_bit2(self):
        self.assertIn("SUSI_WDT_FLAG_SUPPORT_SCI", self.text)
        self.assertIn("-band [UInt32]4", self.code)
        self.assertIn("'unsupported'", self.code)

    def test_sci_uses_the_public_event_type_value(self):
        self.assertIn("$sciEventType = [UInt32]2", self.code)

    def test_sci_start_uses_maximum_delay_and_event_time_and_no_reset(self):
        self.assertIn("$sciDelay = [UInt32]$test.caps.delay_max", self.code)
        self.assertIn("$sciEvent = [UInt32]$test.caps.event_max", self.code)
        self.assertIn("$sciReset = [UInt32]0", self.code)

    def test_sci_reads_event_type_back_and_reports_each_state(self):
        self.assertIn("sci_readback", self.code)
        for state in ("success", "fail", "unsupported", "not_run"):
            self.assertIn(f"'{state}'", self.code)

    def test_sci_stop_is_in_finally_with_retries(self):
        block = self.text[self.text.index("SCI start/stop test"):]
        self.assertIn("} finally {", block)
        self.assertIn("SusiWDogStop", block)
        self.assertIn("$attempt -le 3", block)

    def test_comment_says_what_is_verified_and_what_is_left_to_dqa(self):
        self.assertIn("DQA", self.text)
        self.assertIn("never waits", self.text)

    def test_other_event_types_are_not_started(self):
        code_after = self.code[self.code.index("$sciEventType"):]
        self.assertNotIn("SUSI_WDT_EVENT_TYPE_PWRBTN", code_after)
        self.assertNotIn("$pwrbtnEventType", self.code)
        self.assertNotIn("$irqEventType", self.code)

    def test_still_no_sleep_and_no_pwrcycle(self):
        self.assertNotIn("Start-Sleep", self.code)
        self.assertNotIn("PWRCYCLE", self.code)


class WdtSciNoteTests(unittest.TestCase):
    def note(self, sci_states):
        report = {"metrics": {"start_stop_test": {
            ch: {"sci": {"result": state}} for ch, state in sci_states.items()
        }}}
        return full._section_detail_note("WDT", report)

    def test_all_success(self):
        self.assertEqual(self.note({"WDT1": "success"}), "sci: success")

    def test_unsupported(self):
        self.assertEqual(self.note({"WDT1": "unsupported"}), "sci: unsupported")

    def test_failure_names_the_watchdog(self):
        self.assertEqual(self.note({"WDT1": "fail", "WDT2": "success"}), "sci: WDT1 fail")

    def test_report_without_sci_has_no_note(self):
        report = {"metrics": {"start_stop_test": {"WDT1": {"result": "PASS"}}}}
        self.assertEqual(full._section_detail_note("WDT", report), "")


class Phase2RecommendationTests(unittest.TestCase):
    def test_wdt_recommendation_separates_what_was_checked_from_dqa_work(self):
        text = full.PHASE2_RECOMMENDATIONS["WDT"]
        self.assertIn("Software check only proved", text)
        self.assertIn("SCI", text)
        self.assertIn("recovery harness", text)

    def test_thermalprotect_recommendation_names_all_acting_events(self):
        text = full.PHASE2_RECOMMENDATIONS["ThermalProtect"]
        self.assertIn("read back", text)
        for word in ("THROTTLE", "SHUTDOWN", "POWEROFF"):
            self.assertIn(word, text)


if __name__ == "__main__":
    unittest.main()
