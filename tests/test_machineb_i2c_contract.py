import importlib.util
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
BUILDER_PATH = ROOT / "build_machineB_section_configs.py"
GENERATOR_PATH = ROOT / "susi_gen.py"
RUNNER_PATH = ROOT / "targetB_task" / "machineB_validation" / "run_i2c_validation.ps1"
COMMON_PATH = ROOT / "targetB_task" / "machineB_validation" / "common_susi.ps1"


def load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


builder = load_module("machineb_builder_i2c", BUILDER_PATH)
generator = load_module("susi_generator_i2c", GENERATOR_PATH)


class MachineBI2CContractTests(unittest.TestCase):
    def test_i2c_is_registered_as_machineb_output(self):
        self.assertEqual(builder.SECTION_OUTPUTS["I2C"], "{model}_i2c.json")

    def test_i2c_builder_separates_ini_channel_from_public_api_id(self):
        with tempfile.TemporaryDirectory() as td:
            ini = Path(td) / "BOARD_I2C.ini"
            ini.write_text(
                "[I2C]\n"
                "Channel1=0x12345678,0x80000000,0,0x20000000,\n"
                "Channel2=0x12345678,0x80000001,0,0x20000000,\n",
                encoding="utf-8",
            )
            keys = builder.read_ini_section(ini, "I2C", 4)
            config = builder.build_i2c_config("BOARD", ini, keys, {})

        self.assertEqual(config["required_channels"], ["Channel1", "Channel2"])
        self.assertEqual(config["channels"]["Channel1"]["encoded_channel"], "0x80000000")
        self.assertEqual(config["channels"]["Channel1"]["i2c_api_id"], "0x00000000")
        self.assertEqual(config["channels"]["Channel1"]["capability_bit"], 0)
        self.assertEqual(config["channels"]["Channel2"]["i2c_api_id"], "0x00000001")
        self.assertEqual(config["capability_check"]["supported_id"], "0x00030100")
        self.assertTrue(config["transaction_policy"]["require_fixture_for_transaction"])
        self.assertFalse(config["transaction_policy"]["enabled"])

    def test_generator_assigns_unique_channel_keys_from_encoded_db_channels(self):
        db_result = {
            "status": "FOUND",
            "rows": [
                {"channel": f"0x{0x80000000 + index:08X}", "hardware_id": "0x12345678"}
                for index in range(4)
            ],
        }
        spec = {"features": {"i2c": True}}
        with patch.object(generator, "query_section", return_value=db_result):
            result = generator._build_i2c_query_result(
                Path("unused.db"), "MIO", "EIO-211", {}, spec
            )

        self.assertEqual(
            [row["item_name"] for row in result["rows"]],
            ["Channel1", "Channel2", "Channel3", "Channel4"],
        )

    def test_generator_filters_db_i2c_maximum_set_by_supported_probe_buses(self):
        db_result = {
            "status": "FOUND",
            "rows": [
                {
                    "channel": f"0x{0x80000000 + index:08X}",
                    "hardware_id": "0x12345678",
                    "report_name": report_name,
                }
                for index, report_name in enumerate(
                    ["I2C_EXTERNAL", "I2C_OEM0", "I2C_OEM1", "I2C_OEM2"]
                )
            ],
        }
        probe_spec = {
            "i2c_buses": [
                {"name": "I2C_EXTERNAL", "id": 0, "probe_id": 0},
                {"name": "I2C_OEM0", "id": 0, "probe_id": 1},
            ]
        }
        spec = {"features": {"i2c": True}}
        with patch.object(generator, "query_section", return_value=db_result):
            result = generator._build_i2c_query_result(
                Path("unused.db"), "MIO", "EIO-211", probe_spec, spec
            )

        self.assertEqual(
            [row["item_name"] for row in result["rows"]],
            ["Channel1", "Channel2"],
        )
        self.assertEqual(
            [row["report_name"] for row in result["rows"]],
            ["I2C_EXTERNAL", "I2C_OEM0"],
        )
        self.assertEqual(result["i2c_skipped_oem_ids"], [2, 3])

    def test_generator_never_composes_i2c_channels_from_probe(self):
        # Values come only from DB rows; the probe may only remove rows.
        db_result = {
            "status": "FOUND",
            "rows": [{"channel": "", "hardware_id": "0x12345678"}],
        }
        probe_spec = {
            "i2c_buses": [
                {"name": "I2C_OEM0", "id": 0, "probe_id": 1},
                {"name": "I2C_OEM1", "id": 1, "probe_id": 2},
            ]
        }
        spec = {"features": {"i2c": True}}
        with patch.object(generator, "query_section", return_value=db_result):
            result = generator._build_i2c_query_result(
                Path("unused.db"), "MIO", "EIO-211", probe_spec, spec
            )

        self.assertEqual(result["rows"], [])
        self.assertEqual(result["status"], "SECTION_EMPTY")
        self.assertNotIn("i2c_probe_oem_ids", result)

    def test_generator_keeps_section_empty_when_db_has_no_i2c_rows(self):
        db_result = {"status": "SECTION_EMPTY", "rows": [], "row_count": 0}
        probe_spec = {"i2c_buses": [{"name": "I2C_OEM0", "id": 0, "probe_id": 1}]}
        spec = {"features": {"i2c": True}}
        with patch.object(generator, "query_section", return_value=db_result):
            result = generator._build_i2c_query_result(
                Path("unused.db"), "MIO", "EIO-211", probe_spec, spec
            )

        self.assertEqual(result["status"], "SECTION_EMPTY")
        self.assertEqual(result["rows"], [])

    def test_runner_exercises_frequency_set_readback_restore_without_data_writes(self):
        runner = RUNNER_PATH.read_text(encoding="utf-8")
        common = COMMON_PATH.read_text(encoding="utf-8")

        self.assertIn("SUSI_ID_I2C_SUPPORTED", runner)
        self.assertIn("SusiI2CGetFrequency", runner)
        self.assertIn("Apply-VerdictPolicy", runner)
        self.assertIn("PENDING_FIXTURE", runner)
        self.assertIn("SusiI2CSetFrequency", runner)
        self.assertIn(":restore", runner)
        self.assertIn("SusiI2CProbeDevice", runner)
        self.assertNotIn("SusiI2CWriteTransfer", runner)
        self.assertNotIn("SusiI2CWriteReadCombine", runner)
        self.assertIn("SusiI2CGetCaps", common)
        self.assertIn("SusiI2CGetFrequency", common)
        self.assertIn("SusiI2CSetFrequency", common)
        self.assertIn("SusiI2CProbeDevice", common)

if __name__ == "__main__":
    unittest.main()
