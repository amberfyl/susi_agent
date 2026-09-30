import importlib.util
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SUSI_GEN_PATH = ROOT / "susi_gen.py"

spec = importlib.util.spec_from_file_location("susi_gen_module", SUSI_GEN_PATH)
assert spec is not None and spec.loader is not None
susi_gen = importlib.util.module_from_spec(spec)
spec.loader.exec_module(susi_gen)


class SusiGenBacklightCaseTests(unittest.TestCase):
    def test_backlight_ini_keys_use_exact_case_sensitive_names(self):
        lines = susi_gen._render_section_lines(
            "VGA.Backlight",
            {
                "prod_chip": {"hardware_id": "0x30313640"},
                "rows": [
                    {"item_name": "BACKLIGHT_1", "channel": "0x80000000", "io_port": "0", "option": "0xA0000000"},
                    {"item_name": "BACKLIGHT_2", "channel": "0x80000001", "io_port": "0", "option": "0xA0000000"},
                ],
            },
        )

        self.assertIn("Backlight1=0x30313640,0x80000000,0,0xA0000000,", lines)
        self.assertIn("Backlight2=0x30313640,0x80000001,0,0xA0000000,", lines)
        self.assertNotIn("BACKLIGHT1=0x30313640,0x80000000,0,0xA0000000,", lines)
        self.assertNotIn("BACKLIGHT2=0x30313640,0x80000001,0,0xA0000000,", lines)


if __name__ == "__main__":
    unittest.main()
