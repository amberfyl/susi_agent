import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
RUNNER_PATH = ROOT / "targetB_task" / "machineB_validation" / "run_boardinfo_validation.ps1"


class BoardInfoRunnerTests(unittest.TestCase):
    def setUp(self):
        self.assertTrue(RUNNER_PATH.is_file(), "run_boardinfo_validation.ps1 is missing")
        self.text = RUNNER_PATH.read_text(encoding="utf-8")

    def test_reads_boot_counter_and_running_time_meter_by_public_id(self):
        self.assertIn("SUSI_ID_BOARD_BOOT_COUNTER_VAL", self.text)
        self.assertIn("0x00000001", self.text)
        self.assertIn("SUSI_ID_BOARD_RUNNING_TIME_METER_VAL", self.text)
        self.assertIn("0x00000002", self.text)
        self.assertIn("Read-BoardValue", self.text)

    def test_value_must_be_at_least_one(self):
        self.assertIn("-lt 1", self.text)
        self.assertIn("expected >= 1", self.text)

    def test_api_failure_and_bad_value_are_reported_separately(self):
        self.assertIn("API status", self.text)
        self.assertIn("value is 0", self.text)
        self.assertIn("FAIL_API", self.text)
        self.assertIn("FAIL_READBACK", self.text)

    def test_each_item_reports_success_or_fail(self):
        self.assertIn("metrics.items", self.text)
        self.assertIn("'success'", self.text)
        self.assertIn("'fail'", self.text)

    def test_script_is_read_only(self):
        for name in ("SusiBoardSetValue", "SetConfig", "SetLevel", "SetDirection"):
            self.assertNotIn(name, self.text)


if __name__ == "__main__":
    unittest.main()
