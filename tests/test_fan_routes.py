import importlib.util
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
GENERATOR_PATH = ROOT / "susi_gen.py"


def load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


generator = load_module("susi_generator_fan_routes", GENERATOR_PATH)

PROD = {"id": 1, "product_name": "MIO", "chip_name": "EIO-300", "hardware_id": "0x30313640", "config_chip": ""}
DB_FAN_ROWS = {
    "status": "FOUND",
    "rows": [
        {"report_name": "SUSI_ID_HWM_FAN_CPU", "channel_id": "0x80000000", "io_port": "0", "options": "0x80000000", "pulses": "0"},
        {"report_name": "SUSI_ID_HWM_FAN_SYSTEM", "channel_id": "0x80000001", "io_port": "0", "options": "0x80000000", "pulses": "0"},
        {"report_name": "SUSI_ID_HWM_FAN_CPU2", "channel_id": "0x80000002", "io_port": "0", "options": "0x80000000", "pulses": "0"},
        {"report_name": "SUSI_ID_HWM_FAN_OEM1", "channel_id": "0x80000004", "io_port": "0", "options": "0x80000000", "pulses": "0"},
    ],
}
# Schematic pairing that must be ignored on EC routes (TA4/PWM4 are EC pin numbers).
EC_PAIRING = {"status": "FAN_ONE_TO_MANY_CONFIRMED", "by_key": {"FCPU": {"fanin_idx": 4, "control_idx": 4}}}


class FanDbRowKeyTests(unittest.TestCase):
    def test_exact_report_name_mapping(self):
        key = generator._fan_db_row_key
        self.assertEqual(key({"report_name": "SUSI_ID_HWM_FAN_CPU"}), "FCPU")
        self.assertEqual(key({"report_name": "SUSI_ID_HWM_FAN_CPU2"}), "FCPU2")
        self.assertEqual(key({"report_name": "SUSI_ID_HWM_FAN_SYSTEM"}), "FSYS")
        self.assertEqual(key({"report_name": "SUSI_ID_HWM_FAN_OEM3"}), "FOEM3")


class EcFanRouteTests(unittest.TestCase):
    def _fan(self, probe_fans, db=DB_FAN_ROWS):
        with patch.object(generator, "_load_prod_chip_record", return_value=PROD), \
                patch.object(generator, "query_section", return_value=db):
            return generator._build_hwm_fan_query_result(
                Path("unused.db"), "MIO", "EIO-300",
                probe_spec={"hwm": {"fans": probe_fans}},
                fan_pairing=EC_PAIRING,
                fan_name_hints={"by_key": {"FCPU": "CPU FAN Speed"}},
                is_ec=True,
            )

    def test_ec_fan_uses_db_rows_filtered_by_probe(self):
        result = self._fan(["FCPU"])
        self.assertEqual(result["status"], "FOUND")
        self.assertEqual(len(result["rows"]), 1)
        row = result["rows"][0]
        self.assertEqual(row["item_name"], "FCPU")
        self.assertEqual(row["channel"], "0x80000000")  # DB value, not 0x80000000 + pairing idx 4
        self.assertEqual((row["io_port"], row["option"], row["offset"]), ("0", "0x80000000", "0"))
        self.assertEqual(row["disp_name"], "CPU FAN Speed")

    def test_ec_fan_without_db_rows_is_empty(self):
        result = self._fan(["FCPU"], db={"status": "SECTION_EMPTY", "rows": []})
        self.assertEqual(result["status"], "SECTION_EMPTY")
        self.assertEqual(result["rows"], [])

    def test_ec_fan_control_mirrors_fan_and_ignores_pairing(self):
        fan_result = self._fan(["FCPU", "FSYS"])
        with patch.object(generator, "_fan_template_by_chip",
                          return_value={"io_port": "0", "options": "0x80000000", "pulses": "0"}):
            control, decision = generator._build_hwm_fan_control_query_result(
                Path("unused.db"), fan_result, EC_PAIRING, is_ec=True
            )
        self.assertEqual([r["channel"] for r in control["rows"]], ["0x80000000", "0x80000001"])
        self.assertTrue(all(r["option"] == "0x20000000" for r in control["rows"]))
        self.assertIsNone(decision["pairing_status"])


class BiosFanNameTests(unittest.TestCase):
    def _hints(self, text):
        with tempfile.TemporaryDirectory() as td:
            cache = Path(td) / "c.json"
            cache.write_text(json.dumps({"items": [{"filename": "bios1.jpg", "analysis_text": text}]}),
                             encoding="utf-8")
            return generator._load_fan_name_hints_from_bios_cache(cache)["by_key"]

    def test_uses_bios_label_text(self):
        self.assertEqual(self._hints("FAN_VALUE: CPU FAN Speed=0RPM. No other rows.")["FCPU"], "CPU FAN Speed")
        self.assertEqual(self._hints("CPU FAN Speed 0 RPM; VBAT +3.01 V")["FCPU"], "CPU FAN Speed")
        self.assertEqual(self._hints("FAN_VALUE: System FAN Speed=1200RPM")["FSYS"], "System FAN Speed")

    def test_com_module_labels_still_supported(self):
        self.assertEqual(self._hints("Smart Fan - COM Module")["FCPU"], "COM Module FAN")


if __name__ == "__main__":
    unittest.main()
