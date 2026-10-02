import json
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
REGISTRY_PATH = (
    ROOT
    / "targetB_task"
    / "machineB_validation"
    / "fallback_candidate_registry.json"
)


class FallbackCandidateRegistryTests(unittest.TestCase):
    def test_registry_contains_the_approved_section_whitelists(self):
        from machineb_fallback import load_candidate_registry

        registry = load_candidate_registry(REGISTRY_PATH)

        self.assertEqual(registry.schema_version, "machineb.fallback_candidate_registry.v1")
        self.assertEqual(
            set(registry.sections),
            {
                "SMBus",
                "I2C",
                "VGA.Backlight",
                "VGA.Brightness",
                "HWM.Voltage",
                "HWM.Current",
                "HWM.Temperature",
                "HWM.Fan",
                "HWM.CaseOpen",
                "WDT",
                "GPIO",
                "StorageArea",
                "ThermalProtect",
            },
        )
        self.assertNotIn("HWM.Fan.Control", registry.sections)
        self.assertEqual(
            registry.sections["HWM.CaseOpen"].route_candidates,
            ("0x2E", "0x4E", "0x9E"),
        )
        self.assertEqual(
            registry.sections["GPIO"].option_candidates,
            ("0x20000003", "0xA0000003"),
        )

    def test_option_candidates_are_documented_but_disabled(self):
        from machineb_fallback import load_candidate_registry

        registry = load_candidate_registry(REGISTRY_PATH)

        for section, entry in registry.sections.items():
            with self.subTest(section=section):
                self.assertTrue(entry.route_fallback_enabled)
                self.assertFalse(entry.option_fallback_enabled)
                self.assertGreater(len(entry.option_candidates), 0)

    def test_route_candidates_exclude_baseline_and_numeric_duplicates(self):
        from machineb_fallback import load_candidate_registry, route_candidates_for

        registry = load_candidate_registry(REGISTRY_PATH)

        self.assertEqual(
            route_candidates_for(registry, "GPIO", baseline_value="0"),
            ("1", "0x2E", "0x4E", "0x40", "0x42", "0x44", "0x46", "0x48", "0x4A", "0x4C"),
        )
        self.assertEqual(
            route_candidates_for(registry, "HWM.Temperature", baseline_value="0x2e"),
            ("0", "0x4E"),
        )
        self.assertEqual(
            route_candidates_for(registry, "VGA.Backlight", baseline_value="0x00"),
            (),
        )

    def test_loader_rejects_enabled_option_fallback(self):
        from machineb_fallback import FallbackRegistryError, load_candidate_registry

        payload = {
            "schema_version": "machineb.fallback_candidate_registry.v1",
            "sections": {
                "HWM.Temperature": {
                    "route_field": "IOPort/Address",
                    "route_candidates": ["0", "0x2E"],
                    "option_candidates": ["0x80000001"],
                    "route_fallback_enabled": True,
                    "option_fallback_enabled": True,
                }
            },
        }
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "registry.json"
            path.write_text(json.dumps(payload), encoding="utf-8")

            with self.assertRaisesRegex(FallbackRegistryError, "option fallback must remain disabled"):
                load_candidate_registry(path)

    def test_loader_rejects_duplicate_numeric_route_values(self):
        from machineb_fallback import FallbackRegistryError, load_candidate_registry

        payload = {
            "schema_version": "machineb.fallback_candidate_registry.v1",
            "sections": {
                "GPIO": {
                    "route_field": "IOPort/Address",
                    "route_candidates": ["0", "0x00"],
                    "option_candidates": ["0x20000003"],
                    "route_fallback_enabled": True,
                    "option_fallback_enabled": False,
                }
            },
        }
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "registry.json"
            path.write_text(json.dumps(payload), encoding="utf-8")

            with self.assertRaisesRegex(FallbackRegistryError, "duplicate numeric route candidate"):
                load_candidate_registry(path)


if __name__ == "__main__":
    unittest.main()
