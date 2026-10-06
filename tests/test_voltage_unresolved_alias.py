import json
import tempfile
import unittest
from pathlib import Path

from susi_gen import (
    _assign_oem_slots_for_unmapped_voltage_rows,
    _merge_voltage_alias_into_rows,
    _pick_alias_from_bios_labels,
)


class DcInputAliasTests(unittest.TestCase):
    def test_dc_rail_matches_bios_vin_label_as_written(self):
        alias, reason = _pick_alias_from_bios_labels("HWM_VOLTAGE_DC", {"+Vin", "+5V", "VBAT"})
        self.assertEqual(alias, "+Vin")
        self.assertEqual(reason, "BIOS_LABEL_MATCH_DC_INPUT")
        self.assertEqual(_pick_alias_from_bios_labels("HWM_VOLTAGE_DC", {"DC IN"})[0], "DC IN")

    def test_dc_standby_is_not_taken_as_dc_input(self):
        self.assertIsNone(_pick_alias_from_bios_labels("HWM_VOLTAGE_DCSTBY", {"+Vin"})[0])

    def test_dc_without_vin_label_stays_unresolved(self):
        alias, reason = _pick_alias_from_bios_labels("HWM_VOLTAGE_DC", {"+12V", "+5V"})
        self.assertIsNone(alias)
        self.assertEqual(reason, "NO_CONFIDENT_ALIAS_DC")


class UnresolvedAliasKeepsKeyTests(unittest.TestCase):
    def _bridge(self, payload: dict) -> Path:
        td = tempfile.TemporaryDirectory()
        self.addCleanup(td.cleanup)
        path = Path(td.name) / "bridge.json"
        path.write_text(json.dumps(payload), encoding="utf-8")
        return path

    def test_unresolved_dc_keeps_key_and_empty_name(self):
        query = {
            "rows": [
                {"report_name": "HWM_VOLTAGE_5V", "channel_id": "0x0000F020", "item_name": "V50"},
                {"report_name": "HWM_VOLTAGE_DC", "channel_id": "0x0000F010", "item_name": "DC"},
            ],
        }
        bridge = self._bridge({
            "alias_map": [{
                "report_name": "HWM_VOLTAGE_5V", "channel_id": "0x0000F020",
                "alias": "+5V", "confidence": "high",
            }],
            "unresolved": [{
                "report_name": "HWM_VOLTAGE_DC", "channel_id": "0x0000F010",
                "reason": "NO_CONFIDENT_ALIAS_DC",
            }],
        })

        result = _merge_voltage_alias_into_rows(query, bridge)

        dc = result["rows"][1]
        self.assertEqual(dc["item_name"], "DC")
        self.assertFalse(str(dc.get("disp_name") or "").strip())
        self.assertNotIn("VOEM", json.dumps(result["rows"]))
        self.assertEqual(result["alias_unresolved_count"], 1)
        self.assertEqual(
            result["alias_unresolved"],
            [{"report_name": "HWM_VOLTAGE_DC", "channel_id": "0x0000F010", "item_name": "DC"}],
        )
        self.assertEqual(result["rows"][0]["disp_name"], "+5V")


class OemSlotFallbackTests(unittest.TestCase):
    def test_rows_with_a_susi_id_keep_their_key_even_without_bios_name(self):
        query = {"rows": [
            {"report_name": "HWM_VOLTAGE_DC", "channel_id": "0x10", "item_name": "DC"},
            {"report_name": "HWM_VOLTAGE_12NV", "channel_id": "0x11", "item_name": "12NV"},
            {"report_name": "HWM_VOLTAGE_12VS5", "channel_id": "0x12", "item_name": "12VS5"},
        ]}
        result = _assign_oem_slots_for_unmapped_voltage_rows(query)
        self.assertEqual([r["item_name"] for r in result["rows"]], ["DC", "12NV", "12VS5"])
        self.assertEqual(result["voltage_oem_slots"], [])

    def test_row_without_any_susi_id_goes_to_next_free_oem_slot(self):
        query = {"rows": [
            {"report_name": "HWM_VOLTAGE_OEM0", "channel_id": "0x10", "item_name": "VOEM0"},
            {"report_name": "", "channel_id": "0x11", "item_name": "VIN2", "disp_name": "+Vsys"},
            {"report_name": "", "channel_id": "0x12", "item_name": "VACC"},
        ]}
        result = _assign_oem_slots_for_unmapped_voltage_rows(query)
        rows = result["rows"]
        self.assertEqual(rows[0]["item_name"], "VOEM0")
        self.assertEqual((rows[1]["item_name"], rows[1]["disp_name"]), ("VOEM1", "+Vsys"))
        self.assertEqual((rows[2]["item_name"], rows[2]["disp_name"]), ("VOEM2", "OEM Voltage"))
        self.assertEqual([x["slot"] for x in result["voltage_oem_slots"]], ["VOEM1", "VOEM2"])

    def test_no_free_slot_is_reported_not_guessed(self):
        rows = [{"report_name": f"HWM_VOLTAGE_OEM{i}", "channel_id": str(i), "item_name": f"VOEM{i}"}
                for i in range(4)]
        rows.append({"report_name": "", "channel_id": "9", "item_name": "VACC"})
        result = _assign_oem_slots_for_unmapped_voltage_rows({"rows": rows})
        self.assertEqual(result["rows"][4]["item_name"], "VACC")
        self.assertEqual(result["voltage_unplaced"][0]["from_key"], "VACC")


if __name__ == "__main__":
    unittest.main()
