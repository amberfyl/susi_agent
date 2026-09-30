import importlib.util
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
BUILDER_PATH = ROOT / "build_machineB_section_configs.py"
RUNNER_PATH = ROOT / "targetB_task" / "machineB_validation" / "run_gpio_validation.ps1"
COMMON_PATH = ROOT / "targetB_task" / "machineB_validation" / "common_susi.ps1"

spec = importlib.util.spec_from_file_location("machineb_builder", BUILDER_PATH)
assert spec is not None and spec.loader is not None
builder = importlib.util.module_from_spec(spec)
spec.loader.exec_module(builder)


class MachineBGpioContractTests(unittest.TestCase):
    def test_gpio_is_registered_as_a_machineb_output(self):
        self.assertEqual(builder.SECTION_OUTPUTS["GPIO"], "{model}_gpio.json")

    def test_gpio_builder_separates_public_id_from_ini_group_pin(self):
        with tempfile.TemporaryDirectory() as td:
            ini = Path(td) / "BOARD_GPIO.ini"
            ini.write_text(
                "[GPIO]\n"
                "GPIO00=0x12345678,0,0,0xA0000003,3,7,\n"
                "GPIO08=0x12345678,0,0,0xA0000003,1,0,\n",
                encoding="utf-8",
            )
            keys = builder.read_ini_section(ini, "GPIO", 6)
            config = builder.build_gpio_config("BOARD", ini, keys, {})

        self.assertEqual(config["category"], "GPIO")
        self.assertEqual(config["required_channels"], ["GPIO00", "GPIO08"])
        self.assertEqual(config["channels"]["GPIO00"]["gpio_api_id"], "0x00000000")
        self.assertEqual(config["channels"]["GPIO00"]["tuple_group"], 3)
        self.assertEqual(config["channels"]["GPIO00"]["tuple_pin"], 7)
        self.assertEqual(config["channels"]["GPIO08"]["gpio_api_id"], "0x00000008")
        self.assertEqual(config["channels"]["GPIO08"]["bank_id"], "0x00010000")
        self.assertEqual(config["channels"]["GPIO08"]["bank_bitmask"], "0x00000100")
        self.assertEqual(config["banks"]["Bank0"]["expected_mask"], "0x00000101")
        self.assertTrue(config["functional_check"]["enabled"])
        self.assertTrue(config["safety"]["requires_explicit_functional_switch"])

    def test_runner_has_explicit_write_gate_and_restore_path(self):
        text = RUNNER_PATH.read_text(encoding="utf-8")
        self.assertIn("[switch]$EnableFunctionalTest", text)
        self.assertIn("SusiGPIOGetCaps", text)
        self.assertIn("SusiGPIOGetDirection", text)
        self.assertIn("SusiGPIOGetLevel", text)
        self.assertIn("SusiGPIOSetDirection", text)
        self.assertIn("SusiGPIOSetLevel", text)
        self.assertIn("restore", text.lower())
        self.assertIn("expected_mask", text)

    def test_common_wrapper_declares_gpio_api(self):
        text = COMMON_PATH.read_text(encoding="utf-8")
        for name in (
            "SusiGPIOGetCaps",
            "SusiGPIOGetDirection",
            "SusiGPIOSetDirection",
            "SusiGPIOGetLevel",
            "SusiGPIOSetLevel",
        ):
            self.assertIn(name, text)


if __name__ == "__main__":
    unittest.main()
