import tempfile
import unittest
from pathlib import Path

import build_machineB_section_configs as builder

# SUSI_ID_HWM_VOLTAGE_* offsets from SUSI_ID_HWM_VOLTAGE_BASE (0x00021000) in Susi4.h.
SDK_VOLTAGE_OFFSETS = {
    "VCORE": 0, "VCORE2": 1, "2V5": 2, "3V3": 3, "5V": 4, "12V": 5, "5VSB": 6,
    "3VSB": 7, "VBAT": 8, "5NV": 9, "12NV": 10, "VTT": 11, "24V": 12, "DC": 13,
    "DCSTBY": 14, "VBATLI": 15, "OEM0": 16, "OEM1": 17, "OEM2": 18, "OEM3": 19,
    "1V05": 20, "1V5": 21, "1V8": 22, "12VS5": 23, "5VS5": 24, "3V3S5": 25,
}

# INI item keys emitted by susi_gen.py (_canonical_voltage_item_name) -> SDK name.
INI_KEY_TO_SDK = {
    "VCORE": "VCORE", "VCORE2": "VCORE2", "V25": "2V5", "V33": "3V3", "V50": "5V",
    "V120": "12V", "V5SB": "5VSB", "V3SB": "3VSB", "VBAT": "VBAT", "VN50": "5NV",
    "VN120": "12NV", "VTT": "VTT", "V240": "24V", "DC": "DC", "DCSTBY": "DCSTBY",
    "VBATLI": "VBATLI", "VOEM0": "OEM0", "VOEM1": "OEM1", "VOEM2": "OEM2",
    "VOEM3": "OEM3", "V105": "1V05", "V15": "1V5", "V18": "1V8",
}


class VoltageApiIndexTests(unittest.TestCase):
    def test_sdk_names_match_susi4_h(self):
        for name, offset in SDK_VOLTAGE_OFFSETS.items():
            with self.subTest(name=name):
                self.assertEqual(builder.VOLTAGE_API_INDEX[name], offset)

    def test_every_generator_ini_key_maps_to_the_sdk_id(self):
        for key, sdk_name in INI_KEY_TO_SDK.items():
            with self.subTest(key=key):
                self.assertEqual(builder.VOLTAGE_API_INDEX[key], SDK_VOLTAGE_OFFSETS[sdk_name])


class VoltageConfigLookupTests(unittest.TestCase):
    def build(self, ini_lines):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "BOARD_HWM.Voltage.ini"
            path.write_text("[HWM.Voltage]\n" + "\n".join(ini_lines) + "\n", encoding="utf-8")
            keys = [line.split("=", 1)[0] for line in ini_lines]
            return builder.build_voltage_config("BOARD", path, keys, {})

    def test_key_decides_api_id_even_when_name_is_empty(self):
        config = self.build([
            "DC=0x30313640,0x0000F010,0,0x80000000,0,0,,0",
            "V33=0x30313640,0x0000F040,0,0x80000000,0,0,,0",
        ])
        self.assertEqual(config["channels"]["DC"]["api_id"], "0x0002100D")
        self.assertEqual(config["channels"]["V33"]["api_id"], "0x00021003")

    def test_key_wins_over_display_name(self):
        # BIOS shows "+Vin" for the DC input rail; the key is authoritative.
        config = self.build(["DC=0x30313640,0x0000F010,0,0x80000000,0,0,+Vin,0"])
        self.assertEqual(config["channels"]["DC"]["api_id"], "0x0002100D")
        self.assertEqual(config["channels"]["DC"]["display_name"], "+Vin")

    def test_upper_rails_use_corrected_offsets(self):
        config = self.build([
            "VOEM3=0x30313640,0x0000F010,0,0x80000000,0,0,,0",
            "V18=0x30313640,0x0000F020,0,0x80000000,0,0,,0",
        ])
        self.assertEqual(config["channels"]["VOEM3"]["api_id"], "0x00021013")
        self.assertEqual(config["channels"]["V18"]["api_id"], "0x00021016")

    def test_unknown_key_and_name_still_blocks(self):
        with self.assertRaises(builder.BuildError):
            self.build(["VXYZ=0x30313640,0x0000F010,0,0x80000000,0,0,Mystery,0"])


if __name__ == "__main__":
    unittest.main()
