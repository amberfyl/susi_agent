from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[1]
RUNNER = ROOT / "targetB_task/machineB_validation/run_smbus_validation.ps1"
COMMON = ROOT / "targetB_task/machineB_validation/common_susi.ps1"


class SmbusFixtureContractTests(unittest.TestCase):
    def test_fixture_mode_is_explicit_opt_in(self):
        text = RUNNER.read_text(encoding="utf-8")
        self.assertIn("[switch]$EnableFixtureTest", text)
        self.assertIn("if ($EnableFixtureTest)", text)
        self.assertIn("SMBus FIXTURE VALIDATION", text)

    def test_legacy_fixture_addresses_and_meaning_are_reported(self):
        text = RUNNER.read_text(encoding="utf-8-sig")
        for encoded, seven_bit in (("0xAC", "0x56"), ("0xAE", "0x57"), ("0x4A", "0x25")):
            self.assertIn(encoded, text)
            self.assertIn(seven_bit, text)
        self.assertIn("fixture is not connected, address mismatch, wiring fault, or transaction/API failure", text)

    def test_common_susi_declares_legacy_transaction_apis(self):
        text = COMMON.read_text(encoding="utf-8")
        for api in (
            "SusiSMBWriteQuick",
            "SusiSMBWriteByte",
            "SusiSMBReadByte",
            "SusiSMBWriteWord",
            "SusiSMBReadWord",
            "SusiSMBSendByte",
            "SusiSMBReceiveByte",
            "SusiSMBWriteBlock",
            "SusiSMBReadBlock",
            "SusiSMBI2CWriteBlock",
            "SusiSMBI2CReadBlock",
        ):
            self.assertIn(api, text)

    def test_fixture_result_is_separate_from_sw_verdict(self):
        text = RUNNER.read_text(encoding="utf-8")
        self.assertIn("fixture_validation", text)
        self.assertIn("FAIL_FIXTURE", text)
        self.assertIn("PASS_FIXTURE", text)
        self.assertIn("$report.validation_layers.L5_functional", text)


if __name__ == "__main__":
    unittest.main()
