import json
import tempfile
import unittest
from pathlib import Path

from run_machineB_full_validation import _not_generated_sections


class NotGeneratedSummaryTests(unittest.TestCase):
    def test_lists_every_section_without_a_generated_ini(self):
        matrix = {"sections": [
            {"section": "WDT", "status": "GENERATED"},
            {"section": "VGA.Brightness", "status": "SKIPPED_NOT_APPLICABLE"},
            {"section": "HWM.CaseOpen", "status": "SKIPPED_NOT_APPLICABLE"},
            {"section": "StorageArea", "status": "SKIPPED_EMPTY_SECTION"},
        ]}
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "m.json"
            path.write_text(json.dumps(matrix), encoding="utf-8")
            self.assertEqual(_not_generated_sections(path),
                             ["VGA.Brightness", "HWM.CaseOpen", "StorageArea"])
            self.assertEqual(_not_generated_sections(Path(td) / "missing.json"), [])


if __name__ == "__main__":
    unittest.main()
