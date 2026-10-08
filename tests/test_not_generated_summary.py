import unittest


class NotGeneratedSummaryTests(unittest.TestCase):
    def test_lists_non_generated_sections_with_reason(self):
        import json
        import tempfile
        from pathlib import Path
        from run_machineB_full_validation import _not_generated_sections

        matrix = {"sections": [
            {"section": "WDT", "status": "GENERATED"},
            {"section": "VGA.Brightness", "status": "SKIPPED_NOT_APPLICABLE",
             "reason_code": "REQUEST_NOT_SELECTED", "reason": "Request form did not select VGA.Brightness"},
            {"section": "HWM.CaseOpen", "status": "SKIPPED_NOT_APPLICABLE",
             "reason_code": "BIOS_EVIDENCE_NOT_FOUND"},
            {"section": "StorageArea", "status": "SKIPPED_EMPTY_SECTION"},
        ]}
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "m.json"
            path.write_text(json.dumps(matrix), encoding="utf-8")
            out = _not_generated_sections(path)
            self.assertEqual([e["section"] for e in out], ["VGA.Brightness", "HWM.CaseOpen", "StorageArea"])
            self.assertEqual(out[0]["label"], "not selected in the request form")
            self.assertEqual(out[1]["label"], "not shown on the BIOS Hardware Monitor page")
            self.assertEqual(out[2]["label"], "no rows after DB query / probe filter")
            self.assertEqual(_not_generated_sections(Path(td) / "missing.json"), [])


if __name__ == "__main__":
    unittest.main()
