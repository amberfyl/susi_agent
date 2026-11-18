import importlib.util
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
RUNNER_PATH = ROOT / "targetB_task" / "machineB_validation" / "run_hwm_voltage_validation.ps1"


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


builder = _load("machineb_builder_voltage_nominal", ROOT / "build_machineB_section_configs.py")


def _config(keys: list[str], policy: dict | None = None) -> dict:
    lines = ["[HWM.Voltage]"]
    for i, key in enumerate(keys):
        lines.append(f'{key}=0x12345678,0x8000000{i},0,0xA0000000,0,0,"{key}",')
    with tempfile.TemporaryDirectory() as td:
        ini = Path(td) / "BOARD_HWM.Voltage.ini"
        ini.write_text("\n".join(lines) + "\n", encoding="utf-8")
        parsed = builder.read_ini_section(ini, "HWM.Voltage", 7)
        return builder.build_voltage_config("BOARD", ini, parsed, policy or {})


class VoltageNominalConfigTests(unittest.TestCase):
    def test_fixed_rails_get_nominal_from_the_api_index(self):
        expected = {
            "V33": 3300, "V50": 5000, "V120": 12000, "V5SB": 5000, "V3SB": 3300,
            "V25": 2500, "VBAT": 3000, "V240": 24000, "V105": 1050, "V15": 1500,
            "V18": 1800,
        }
        channels = _config(list(expected))["channels"]
        for key, nominal in expected.items():
            self.assertEqual(channels[key]["nominal_millivolts"], nominal, key)

    def test_negative_rails_use_absolute_value(self):
        channels = _config(["VN50", "VN120"])["channels"]
        self.assertEqual(channels["VN50"]["nominal_millivolts"], 5000)
        self.assertTrue(channels["VN50"]["use_absolute_value"])
        self.assertEqual(channels["VN120"]["nominal_millivolts"], 12000)
        self.assertFalse(_config(["V50"])["channels"]["V50"]["use_absolute_value"])

    def test_s5_rails(self):
        channels = _config(["12VS5", "5VS5", "3V3S5"])["channels"]
        self.assertEqual(channels["12VS5"]["nominal_millivolts"], 12000)
        self.assertEqual(channels["5VS5"]["nominal_millivolts"], 5000)
        self.assertEqual(channels["3V3S5"]["nominal_millivolts"], 3300)

    def test_variable_rails_have_no_nominal(self):
        channels = _config(["VCORE", "VCORE2", "VTT", "DC", "DCSTBY", "VBATLI", "OEM0", "VOEM1"])["channels"]
        for key, channel in channels.items():
            self.assertIsNone(channel["nominal_millivolts"], key)

    def test_default_tolerance_is_ten_percent_and_policy_can_change_it(self):
        self.assertEqual(_config(["V50"])["read_check"]["nominal_tolerance_percent"], 10.0)
        policy = {"voltage_sampling": {"nominal_tolerance_percent": 5}}
        self.assertEqual(_config(["V50"], policy)["read_check"]["nominal_tolerance_percent"], 5.0)


class VoltageNominalRunnerTests(unittest.TestCase):
    def setUp(self):
        self.text = RUNNER_PATH.read_text(encoding="utf-8")

    def test_runner_checks_against_nominal(self):
        for token in ("nominal_millivolts", "nominal_tolerance_percent", "use_absolute_value",
                      "nominal_ok", "nominalFailures"):
            self.assertIn(token, self.text)

    def test_reason_lists_expected_range_and_actual(self):
        self.assertIn("expected", self.text)

    def test_runner_has_balanced_brackets(self):
        for a, b in ("{}", "[]"):  # "()" skipped: comments like "1)" unbalance it
            self.assertEqual(self.text.count(a), self.text.count(b), a)


class VoltageNoteTests(unittest.TestCase):
    full = _load("machineb_full_voltage_note", ROOT / "run_machineB_full_validation.py")

    def test_note_counts_rated_and_variable_rails(self):
        report = {"metrics": {"channels": {
            "V50": {"nominal_ok": True}, "V120": {"nominal_ok": False}, "VCORE": {"nominal_ok": None},
        }}}
        note = self.full._section_detail_note("HWM.Voltage", report)
        self.assertEqual(note, "rated check: 1/2 fixed rails in tolerance; out of range: V120; 1 variable rails range-only")

    def test_no_rated_rails_is_not_shown_as_zero_of_zero(self):
        report = {"metrics": {"channels": {"VBAT": {"nominal_ok": None}, "DC": {"nominal_ok": None}}}}
        note = self.full._section_detail_note("HWM.Voltage", report)
        self.assertNotIn("0/0", note)
        self.assertIn("no fixed rails rated", note)

    def test_old_report_without_nominal_has_no_note(self):
        report = {"metrics": {"channels": {"V50": {"in_range": True}}}}
        self.assertEqual(self.full._section_detail_note("HWM.Voltage", report), "")


if __name__ == "__main__":
    unittest.main()
