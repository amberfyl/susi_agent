import json
import tempfile
import unittest
from pathlib import Path

import susi_gen


class DividerScaleTests(unittest.TestCase):
    def test_decimal_scales_both_to_integers_keeping_ratio(self):
        self.assertEqual(susi_gen._scale_divider_pair("40.2K", "10K"), ("402", "100"))
        self.assertEqual(susi_gen._scale_divider_pair("5.6K", "1.2K"), ("56", "12"))

    def test_integers_unchanged(self):
        self.assertEqual(susi_gen._scale_divider_pair("30K", "10K"), ("30", "10"))

    def test_unparseable_returns_none(self):
        self.assertIsNone(susi_gen._scale_divider_pair("", "10K"))


class DividerApplyTests(unittest.TestCase):
    def _rows(self):
        def row(item, ch):
            return {"item_name": item, "channel": ch, "resistor1": "0", "resistor2": "0"}
        return {"status": "FOUND", "rows": [
            row("VCORE", "0x00000100"), row("V33", "0x80000000"), row("V50", "0x80000001"),
            row("V120", "0x80000002"), row("V5SB", "0x80000000"), row("VBAT", "0x0000F070")]}

    def _apply(self, routes):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "x.json"
            p.write_text(json.dumps({"routes": routes}), encoding="utf-8")
            return susi_gen._apply_superio_voltage_divider(self._rows(), p)

    def test_vin_order_follows_schematic_not_fixed_order(self):
        out = self._apply([
            {"vin": "VIN0", "rail": "+V12", "net": "SIO_+V12IN", "r1": "110K", "r2": "10K"},
            {"vin": "VIN1", "rail": "+V5", "net": "SIO_+V5IN", "r1": "40.2K", "r2": "10K"},
            {"vin": "VIN2", "rail": "+V5_DUAL", "net": "SIO_+V5SBIN", "r1": "30K", "r2": "10K"}])
        by = {r["item_name"]: r for r in out["rows"]}
        self.assertEqual((by["V120"]["channel"], by["V120"]["resistor1"], by["V120"]["resistor2"]), ("0x80000000", "110", "10"))
        self.assertEqual((by["V50"]["channel"], by["V50"]["resistor1"], by["V50"]["resistor2"]), ("0x80000001", "402", "100"))
        self.assertEqual((by["V5SB"]["channel"], by["V5SB"]["resistor1"]), ("0x80000002", "30"))
        # V33 shared 0x80000000 with a schematic-claimed rail -> dropped; others stay 0/0
        self.assertNotIn("V33", by)
        self.assertEqual((by["VCORE"]["resistor1"], by["VBAT"]["resistor2"]), ("0", "0"))

    def test_missing_evidence_keeps_rows_and_flags_insufficient(self):
        out = susi_gen._apply_superio_voltage_divider(self._rows(), Path("/nonexistent/x.json"))
        self.assertEqual(out["voltage_divider"]["status"], "HWM_VOLTAGE_DIVIDER_EVIDENCE_INSUFFICIENT")
        self.assertEqual(len(out["rows"]), 6)


if __name__ == "__main__":
    unittest.main()
