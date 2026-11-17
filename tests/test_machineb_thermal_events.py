import importlib.util
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
RUNNER_PATH = ROOT / "targetB_task" / "machineB_validation" / "run_thermalprotect_validation.ps1"


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


builder = _load("machineb_builder_thermal_events", ROOT / "build_machineB_section_configs.py")
full = _load("machineb_full_thermal_events", ROOT / "run_machineB_full_validation.py")


def _thermal_config() -> dict:
    with tempfile.TemporaryDirectory() as td:
        ini = Path(td) / "BOARD_ThermalProtect.ini"
        ini.write_text(
            "[ThermalProtect]\n"
            "TPCH0=0x30313640,0x80000000,0,0xA0000000,\n"
            "TPCH1=0x30313640,0x80000001,0,0xA0000000,\n",
            encoding="utf-8",
        )
        keys = builder.read_ini_section(ini, "ThermalProtect", 4)
        return builder.build_thermalprotect_config("BOARD", ini, keys, {})


class ThermalEventConfigTests(unittest.TestCase):
    def test_config_lists_the_four_public_event_types(self):
        events = _thermal_config()["event_check"]["events"]
        self.assertEqual(list(events), ["none", "shutdown", "throttle", "poweroff"])
        self.assertEqual(events["none"]["value"], "0x000000FF")
        self.assertEqual(events["shutdown"]["value"], "0x00000000")
        self.assertEqual(events["throttle"]["value"], "0x00000001")
        self.assertEqual(events["poweroff"]["value"], "0x00000002")

    def test_none_needs_no_support_flag_and_others_use_one_bit_each(self):
        events = _thermal_config()["event_check"]["events"]
        self.assertIsNone(events["none"]["support_flag"])
        self.assertEqual(events["shutdown"]["support_flag"], "0x00000001")
        self.assertEqual(events["throttle"]["support_flag"], "0x00000002")
        self.assertEqual(events["poweroff"]["support_flag"], "0x00000004")

    def test_config_never_uses_the_ec_internal_none_value(self):
        events = _thermal_config()["event_check"]["events"]
        self.assertNotEqual(events["none"]["value"], "0x00000008")


class ThermalEventRunnerTests(unittest.TestCase):
    def setUp(self):
        self.text = RUNNER_PATH.read_text(encoding="utf-8")

    def test_runner_reports_each_event_result(self):
        self.assertIn("metrics.events", self.text)
        for state in ("success", "fail", "unsupported", "not_run"):
            self.assertIn(f"'{state}'", self.text)

    def test_runner_skips_unsupported_events_without_setconfig(self):
        self.assertIn("event_check", self.text)
        self.assertIn("support_flag", self.text)

    def test_runner_restores_after_each_event(self):
        self.assertIn("ThermalEventRestore", self.text)

    def test_event_failure_fails_the_section_with_a_reason(self):
        self.assertIn("eventFailures", self.text)


class ThermalEventVerdictTests(unittest.TestCase):
    def test_failed_event_report_fails_the_section(self):
        report = {
            "category": "ThermalProtect",
            "result": "FAIL_READBACK",
            "validation_layers": {
                "L1_configuration": "PASS",
                "L2_capability": "PASS",
                "L3_api": "PASS",
                "L4_readback": "FAIL",
                "L5_functional": "CONDITIONAL",
                "L6_recovery": "PASS",
            },
        }
        self.assertEqual(full._normalize_report_verdict(report)[0], "FAIL")


if __name__ == "__main__":
    unittest.main()
