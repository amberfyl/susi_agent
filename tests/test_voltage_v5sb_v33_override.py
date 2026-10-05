import json
import tempfile
import unittest
from pathlib import Path


class VoltageV5sbV33OverrideTests(unittest.TestCase):
    def _bridge(self, payload: dict) -> Path:
        td = tempfile.TemporaryDirectory()
        self.addCleanup(td.cleanup)
        path = Path(td.name) / "bridge.json"
        path.write_text(json.dumps(payload), encoding="utf-8")
        return path

    def test_aimb_nct6126d_strong_schematic_override_renames_v5sb_to_v33(self):
        from susi_gen import _merge_voltage_alias_into_rows

        query = {
            "query_key": {"product_name": "AIMB", "chip_name": "NCT6126D"},
            "rows": [{
                "report_name": "HWM_VOLTAGE_5VSB",
                "channel_id": "0x80000003",
                "item_name": "V5SB",
            }],
        }
        bridge = self._bridge({
            "alias_map": [{
                "report_name": "HWM_VOLTAGE_5VSB",
                "channel_id": "0x80000003",
                "alias": "+3.3V",
                "confidence": "high",
                "item_name_override": "V33",
                "reason": "AIMB_NCT6126D_V5SB_RENAMED_TO_V33_BY_SCHEMATIC",
            }],
            "unresolved": [],
        })

        result = _merge_voltage_alias_into_rows(query, bridge)

        self.assertEqual("V33", result["rows"][0]["item_name"])
        self.assertEqual("+3.3V", result["rows"][0]["disp_name"])

    def test_v33_override_is_ignored_outside_aimb_nct6126d(self):
        from susi_gen import _merge_voltage_alias_into_rows

        query = {
            "query_key": {"product_name": "MIO", "chip_name": "NCT6126D"},
            "rows": [{
                "report_name": "HWM_VOLTAGE_5VSB",
                "channel_id": "0x80000003",
                "item_name": "V5SB",
            }],
        }
        bridge = self._bridge({
            "alias_map": [{
                "report_name": "HWM_VOLTAGE_5VSB",
                "channel_id": "0x80000003",
                "alias": "+3.3V",
                "confidence": "high",
                "item_name_override": "V33",
                "reason": "AIMB_NCT6126D_V5SB_RENAMED_TO_V33_BY_SCHEMATIC",
            }],
            "unresolved": [],
        })

        result = _merge_voltage_alias_into_rows(query, bridge)

        self.assertEqual("V5SB", result["rows"][0]["item_name"])

    def test_bridge_builds_v33_override_only_from_structured_net_level_evidence(self):
        from susi_gen import _build_voltage_alias_bridge

        ec_base = {
            "project": "AIMB-TEST",
            "query_key": {"product_name": "AIMB", "chip_name": "NCT6126D"},
            "items": [{
                "report_name": "HWM_VOLTAGE_5VSB",
                "channel_id": "0x80000003",
            }],
        }
        bios_cache = {
            "project": "AIMB-TEST",
            "items": [
                {"filename": "bios1.png", "voltage_label_hints": ["+3.3V"]},
                {
                    "filename": "circuit1.png",
                    "voltage_route_hints": [{
                        "source_item": "V5SB",
                        "actual_rail": "+3.3V",
                        "evidence_level": "NET_LEVEL_CONFIRMED",
                    }],
                },
            ],
        }

        bridge = _build_voltage_alias_bridge(ec_base, bios_cache)

        self.assertEqual(1, len(bridge["alias_map"]))
        self.assertEqual("+3.3V", bridge["alias_map"][0]["alias"])
        self.assertEqual("V33", bridge["alias_map"][0]["item_name_override"])
        self.assertEqual(
            "AIMB_NCT6126D_V5SB_RENAMED_TO_V33_BY_SCHEMATIC",
            bridge["alias_map"][0]["reason"],
        )

    def test_existing_v33_row_wins_over_same_channel_v5sb_override(self):
        from susi_gen import _merge_voltage_alias_into_rows

        query = {
            "query_key": {"product_name": "AIMB", "chip_name": "NCT6126D"},
            "rows": [
                {"report_name": "HWM_VOLTAGE_3V3", "channel_id": "0x80000003", "item_name": "V33"},
                {"report_name": "HWM_VOLTAGE_5VSB", "channel_id": "0x80000003", "item_name": "V5SB"},
            ],
        }
        bridge = self._bridge({
            "alias_map": [{
                "report_name": "HWM_VOLTAGE_5VSB",
                "channel_id": "0x80000003",
                "alias": "+3.3V",
                "confidence": "high",
                "item_name_override": "V33",
                "reason": "AIMB_NCT6126D_V5SB_RENAMED_TO_V33_BY_SCHEMATIC",
            }],
            "unresolved": [],
        })

        result = _merge_voltage_alias_into_rows(query, bridge)

        self.assertEqual(1, len(result["rows"]))
        self.assertEqual("V33", result["rows"][0]["item_name"])
        self.assertEqual("+3.3V", result["rows"][0]["disp_name"])


if __name__ == "__main__":
    unittest.main()