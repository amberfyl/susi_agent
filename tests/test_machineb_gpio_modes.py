import importlib.util
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
RUNNER_PATH = ROOT / "targetB_task" / "machineB_validation" / "run_gpio_validation.ps1"


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


builder = _load("machineb_builder_modes", ROOT / "build_machineB_section_configs.py")
full = _load("machineb_full_modes", ROOT / "run_machineB_full_validation.py")


def _gpio_config() -> dict:
    with tempfile.TemporaryDirectory() as td:
        ini = Path(td) / "BOARD_GPIO.ini"
        ini.write_text(
            "[GPIO]\n"
            "GPIO00=0x12345678,0,0,0xA0000003,3,7,\n"
            "GPIO37=0x12345678,0,0,0xA0000003,1,0,\n",
            encoding="utf-8",
        )
        keys = builder.read_ini_section(ini, "GPIO", 6)
        return builder.build_gpio_config("BOARD", ini, keys, {})


def _report(l5: str, modes: dict | None) -> dict:
    report = {
        "category": "GPIO",
        "result": "PASS",
        "validation_layers": {
            "L1_configuration": "PASS",
            "L2_capability": "PASS",
            "L3_api": "PASS",
            "L4_readback": "PASS",
            "L5_functional": l5,
            "L6_recovery": "PASS",
        },
        "metrics": {},
    }
    if modes is not None:
        report["metrics"]["modes"] = modes
    return report


class GpioConfigModesTests(unittest.TestCase):
    def test_config_requires_both_modes(self):
        config = _gpio_config()
        self.assertEqual(config["functional_check"]["modes"], ["single", "multiple"])

    def test_single_pin_uses_pin_id_and_bitmask_one(self):
        channel = _gpio_config()["channels"]["GPIO37"]
        self.assertEqual(channel["gpio_api_id"], "0x00000025")
        self.assertEqual(channel["single_bitmask"], "0x00000001")
        self.assertEqual(channel["bank_id"], "0x00010001")
        self.assertEqual(channel["bank_bitmask"], "0x00000020")


class GpioRunnerModesTests(unittest.TestCase):
    def setUp(self):
        self.text = RUNNER_PATH.read_text(encoding="utf-8")

    def test_runner_tests_single_pins_by_pin_id(self):
        self.assertIn("gpio_api_id", self.text)
        self.assertIn("single_bitmask", self.text)

    def test_runner_reports_each_mode_result(self):
        self.assertIn("metrics.modes", self.text)
        for state in ("success", "fail", "not_run"):
            self.assertIn(state, self.text)

    def test_runner_cross_checks_single_and_multiple(self):
        self.assertIn("cross", self.text.lower())


class GpioSummaryModesTests(unittest.TestCase):
    def verdict(self, report):
        return full._normalize_report_verdict(report)[0]

    def test_both_modes_success_passes(self):
        modes = {"single": "success", "multiple": "success"}
        self.assertEqual(self.verdict(_report("PASS", modes)), "PASS")

    def test_single_fail_does_not_pass(self):
        modes = {"single": "fail", "multiple": "success"}
        self.assertNotEqual(self.verdict(_report("PASS", modes)), "PASS")

    def test_missing_mode_does_not_pass(self):
        modes = {"single": "not_run", "multiple": "success"}
        self.assertNotEqual(self.verdict(_report("PASS", modes)), "PASS")

    def test_report_without_modes_does_not_pass(self):
        self.assertNotEqual(self.verdict(_report("PASS", None)), "PASS")

    def test_functional_not_requested_stays_conditional(self):
        self.assertEqual(self.verdict(_report("CONDITIONAL", None)), "CONDITIONAL")


if __name__ == "__main__":
    unittest.main()
