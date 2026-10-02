import json
import tempfile
import unittest
from pathlib import Path


FULL_INI = """[Information]
IniVersion=1.0.1.0
[HWM.Temperature]
TCPU=1,0,0x2E,0x80000001,0,
TSYS=1,1,0x2E,0x80000001,0,
[GPIO]
GPIO00=2,0,0x40,0x20000003,0,0,
"""


class ProjectRouteOverrideTests(unittest.TestCase):
    def test_only_converged_sections_are_persisted_atomically(self):
        from machineb_fallback import persist_project_route_overrides

        convergence = {
            "sections": [
                {
                    "section": "HWM.Temperature",
                    "status": "CONVERGED",
                    "selected_route": "0x4E",
                    "attempts": [{"report_sha256": "a" * 64}],
                },
                {
                    "section": "GPIO",
                    "status": "ALL_CANDIDATES_FAILED",
                    "selected_route": None,
                    "attempts": [],
                },
            ]
        }
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "BOARD-config-overrides.json"

            payload = persist_project_route_overrides(
                path,
                project="BOARD",
                run_id="run-1",
                convergence=convergence,
            )

            self.assertEqual(payload["schema_version"], "machineb.project_route_overrides.v1")
            self.assertEqual(set(payload["sections"]), {"HWM.Temperature"})
            self.assertEqual(payload["sections"]["HWM.Temperature"]["route_value"], "0x4E")
            self.assertEqual(payload["sections"]["HWM.Temperature"]["validation_status"], "CONVERGED")
            self.assertEqual(json.loads(path.read_text(encoding="utf-8")), payload)
            self.assertFalse(path.with_suffix(".json.tmp").exists())

    def test_new_success_merges_without_losing_previous_project_override(self):
        from machineb_fallback import persist_project_route_overrides

        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "BOARD-config-overrides.json"
            persist_project_route_overrides(
                path,
                project="BOARD",
                run_id="run-1",
                convergence={"sections": [{
                    "section": "HWM.Temperature",
                    "status": "CONVERGED",
                    "selected_route": "0x4E",
                    "attempts": [],
                }]},
            )
            payload = persist_project_route_overrides(
                path,
                project="BOARD",
                run_id="run-2",
                convergence={"sections": [{
                    "section": "GPIO",
                    "status": "CONVERGED",
                    "selected_route": "0x42",
                    "attempts": [],
                }]},
            )

            self.assertEqual(set(payload["sections"]), {"HWM.Temperature", "GPIO"})

    def test_override_application_updates_full_and_split_ini_only_in_route_field(self):
        from machineb_fallback import apply_project_route_overrides

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            full = root / "BOARD-pre.ini"
            temp_split = root / "BOARD_HWM.Temperature.ini"
            gpio_split = root / "BOARD_GPIO.ini"
            override = root / "BOARD-config-overrides.json"
            full.write_text(FULL_INI, encoding="utf-8")
            temp_split.write_text(
                "[HWM.Temperature]\nTCPU=1,0,0x2E,0x80000001,0,\nTSYS=1,1,0x2E,0x80000001,0,\n",
                encoding="utf-8",
            )
            gpio_split.write_text(
                "[GPIO]\nGPIO00=2,0,0x40,0x20000003,0,0,\n",
                encoding="utf-8",
            )
            override.write_text(json.dumps({
                "schema_version": "machineb.project_route_overrides.v1",
                "project": "BOARD",
                "sections": {
                    "HWM.Temperature": {
                        "route_value": "0x4E",
                        "validation_status": "CONVERGED",
                    }
                },
            }), encoding="utf-8")

            applied = apply_project_route_overrides(
                project="BOARD",
                override_path=override,
                full_ini_path=full,
                section_paths={
                    "HWM.Temperature": temp_split,
                    "GPIO": gpio_split,
                },
            )

            self.assertEqual(applied["status"], "APPLIED")
            self.assertEqual(applied["sections"], ["HWM.Temperature"])
            self.assertIn("0x4E,0x80000001", full.read_text(encoding="utf-8"))
            self.assertIn("0x4E,0x80000001", temp_split.read_text(encoding="utf-8"))
            self.assertEqual(
                gpio_split.read_text(encoding="utf-8"),
                "[GPIO]\nGPIO00=2,0,0x40,0x20000003,0,0,\n",
            )

    def test_override_for_missing_generated_section_is_rejected(self):
        from machineb_fallback import FallbackOverrideError, apply_project_route_overrides

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            full = root / "BOARD-pre.ini"
            override = root / "BOARD-config-overrides.json"
            full.write_text(FULL_INI, encoding="utf-8")
            override.write_text(json.dumps({
                "schema_version": "machineb.project_route_overrides.v1",
                "project": "BOARD",
                "sections": {
                    "I2C": {"route_value": "0x4E", "validation_status": "CONVERGED"}
                },
            }), encoding="utf-8")

            with self.assertRaisesRegex(FallbackOverrideError, "not generated"):
                apply_project_route_overrides(
                    project="BOARD",
                    override_path=override,
                    full_ini_path=full,
                    section_paths={"HWM.Temperature": root / "temp.ini"},
                )


if __name__ == "__main__":
    unittest.main()
