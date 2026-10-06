import re
import unittest
from pathlib import Path

RUNNER_DIR = Path(__file__).resolve().parents[1] / "targetB_task" / "machineB_validation"
RUNNER_PATH = RUNNER_DIR / "run_wdt_validation.ps1"
COMMON_PATH = RUNNER_DIR / "common_susi.ps1"


class MachineBWdtContractTests(unittest.TestCase):
    """Phase 1 WDT test must exercise Start/Stop without ever resetting the target."""

    def setUp(self):
        self.runner = RUNNER_PATH.read_text(encoding="utf-8-sig")

    def test_start_uses_maximum_reset_time_and_no_event(self):
        self.assertIn("$resetTime = [UInt32]$test.caps.reset_max", self.runner)
        # When the current value already equals the maximum, step down one unit
        # so the readback proves the write; never below two units.
        self.assertIn("$resetTime = $resetTime - $unit", self.runner)
        self.assertIn("[UInt32]$test.readback.reset_time -eq $resetTime", self.runner)
        self.assertIn("$eventType = [UInt32]0  # SUSI_WDT_EVENT_TYPE_NONE", self.runner)
        self.assertNotIn("PWRCYCLE", re.sub(r"#.*", "", self.runner))

    def test_stop_is_always_attempted_in_finally_with_retries(self):
        finally_block = self.runner[self.runner.index("} finally {\n                    if ($started)"):]
        self.assertIn("SusiWDogStop", finally_block)
        self.assertIn("$attempt -le 3", finally_block)
        self.assertIn("FAIL_STOP", finally_block)

    def test_running_watchdog_is_left_untouched(self):
        self.assertIn("[Convert]::ToUInt32('FFFFFEFA', 16)", self.runner)
        self.assertIn("ALREADY_RUNNING_NOT_MODIFIED", self.runner)

    def test_runner_never_waits_for_timeout(self):
        code = re.sub(r"#.*", "", self.runner)
        self.assertNotIn("Start-Sleep", code)

    def test_common_declares_wdog_api(self):
        common = COMMON_PATH.read_text(encoding="utf-8-sig")
        for name in ("SusiWDogGetCaps", "SusiWDogStart", "SusiWDogStop", "SusiWDogTrigger"):
            self.assertIn(name, common)


class HexLiteralTests(unittest.TestCase):
    def test_runners_avoid_signed_hex_uint32_literals(self):
        # Windows PowerShell 5.1 parses 0x80000000-0xFFFFFFFF as negative Int32,
        # so [UInt32]0xFFFFFCFF throws at runtime.
        pattern = re.compile(r"\[UInt32\]0x[89A-Fa-f][0-9A-Fa-f]{7}\b")
        offenders = [
            f"{path.name}:{number}"
            for path in sorted(RUNNER_DIR.glob("*.ps1"))
            for number, line in enumerate(path.read_text(encoding="utf-8-sig").splitlines(), 1)
            if pattern.search(line)
        ]
        self.assertEqual(offenders, [])


if __name__ == "__main__":
    unittest.main()
