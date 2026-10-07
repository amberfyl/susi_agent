import tempfile
import unittest
from pathlib import Path

from parse_probe import parse_probe_report
import susi_gen


class VgaProbeChannelParsingTests(unittest.TestCase):
    def _parse(self, text: str) -> dict:
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "BOARD_susi_board_probe_report.txt"
            path.write_text(text, encoding="utf-8")
            return parse_probe_report(path)

    def test_primary_found_rows_define_supported_counts(self):
        spec = self._parse(
            "========== VGA.Brightness Channels ==========\n"
            "[OK]  Brightness1_Backlight 1                    Id=0x00000000 Status=FOUND\n"
            "[OK]  Brightness1_Backlight 1_Max                Id=0x00000000 Value=100\n"
            "[ERR] Brightness2                                Id=0x00000001 Status=SUSI_STATUS_UNSUPPORTED\n"
            "========== VGA.Backlight Channels ==========\n"
            "[OK]  Backlight1_Backlight 1                     Id=0x00000000 Status=FOUND\n"
            "[OK]  Backlight1_Backlight 1_Enable              Id=0x00000000 Value=OFF\n"
            "[ERR] Backlight2                                 Id=0x00000001 Status=SUSI_STATUS_UNSUPPORTED\n"
        )

        self.assertTrue(spec["vga"]["brightness"]["present"])
        self.assertEqual(spec["vga"]["brightness"]["count"], 1)
        self.assertEqual(spec["vga"]["brightness"]["channel_ids"], [0])
        self.assertTrue(spec["vga"]["backlight"]["present"])
        self.assertEqual(spec["vga"]["backlight"]["count"], 1)
        self.assertEqual(spec["vga"]["backlight"]["channel_ids"], [0])


class VgaProbeChannelFilterTests(unittest.TestCase):
    def setUp(self):
        self.result = {
            "status": "FOUND",
            "row_count": 3,
            "rows": [
                {"item_name": "ROW1"},
                {"item_name": "ROW2"},
                {"item_name": "ROW3"},
            ],
        }

    def test_brightness_trims_db_rows_to_probe_supported_count(self):
        filtered, meta = susi_gen._filter_vga_rows_by_probe(
            self.result,
            "VGA.Brightness",
            {"vga": {"brightness": {"present": True, "count": 1, "channel_ids": [0]}}},
        )
        self.assertEqual(filtered["rows"], [{"item_name": "ROW1"}])
        self.assertEqual(filtered["row_count"], 1)
        self.assertEqual(meta["probe_supported_count"], 1)
        self.assertEqual(meta["trimmed_count"], 2)

    def test_backlight_trims_independently(self):
        filtered, meta = susi_gen._filter_vga_rows_by_probe(
            self.result,
            "VGA.Backlight",
            {"vga": {"backlight": {"present": True, "count": 2, "channel_ids": [0, 1]}}},
        )
        self.assertEqual(filtered["row_count"], 2)
        self.assertEqual(meta["trimmed_count"], 1)

    def test_missing_probe_section_preserves_db_maximum_set(self):
        filtered, meta = susi_gen._filter_vga_rows_by_probe(
            self.result,
            "VGA.Brightness",
            {"vga": {"brightness": {"present": False, "count": 0, "channel_ids": []}}},
        )
        self.assertEqual(filtered["row_count"], 3)
        self.assertFalse(meta["filter_applied"])

    def test_brightness_with_zero_supported_channels_keeps_db_rows(self):
        filtered, meta = susi_gen._filter_vga_rows_by_probe(
            self.result,
            "VGA.Brightness",
            {"vga": {"brightness": {"present": True, "count": 0, "channel_ids": []}}},
        )
        self.assertEqual(filtered["row_count"], 3)
        self.assertFalse(meta["filter_applied"])

    def test_present_probe_section_with_no_supported_channel_drops_all_rows(self):
        filtered, meta = susi_gen._filter_vga_rows_by_probe(
            self.result,
            "VGA.Backlight",
            {"vga": {"backlight": {"present": True, "count": 0, "channel_ids": []}}},
        )
        self.assertEqual(filtered["rows"], [])
        self.assertEqual(filtered["row_count"], 0)
        self.assertEqual(filtered["status"], "SECTION_EMPTY")
        self.assertEqual(meta["trimmed_count"], 3)


if __name__ == "__main__":
    unittest.main()
