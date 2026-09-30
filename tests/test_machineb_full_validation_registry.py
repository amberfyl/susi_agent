import importlib.util
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
BUILDER_PATH = ROOT / "build_machineB_section_configs.py"
RUNNER_DIR = ROOT / "targetB_task" / "machineB_validation"


def load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


builder = load_module("machineb_builder_registry", BUILDER_PATH)


class MachineBFullValidationRegistryTests(unittest.TestCase):
    def test_registry_contains_all_14_sections_once(self):
        from run_machineB_full_validation import SECTION_REGISTRY

        sections = [entry.section for entry in SECTION_REGISTRY]

        self.assertEqual(len(sections), 14)
        self.assertEqual(len(set(sections)), 14)
        self.assertEqual(set(sections), set(builder.SECTION_OUTPUTS))

    def test_registry_order_is_fixed_from_easiest_to_most_complex(self):
        from run_machineB_full_validation import SECTION_REGISTRY

        self.assertEqual(
            [entry.section for entry in SECTION_REGISTRY],
            [
                "HWM.CaseOpen",
                "HWM.Current",
                "HWM.Voltage",
                "HWM.Temperature",
                "I2C",
                "SMBus",
                "WDT",
                "ThermalProtect",
                "HWM.Fan",
                "HWM.Fan.Control",
                "VGA.Backlight",
                "VGA.Brightness",
                "GPIO",
                "StorageArea",
            ],
        )
        self.assertEqual(
            [entry.difficulty_rank for entry in SECTION_REGISTRY], list(range(1, 15))
        )

    def test_registry_config_templates_match_the_builder(self):
        from run_machineB_full_validation import SECTION_REGISTRY

        for entry in SECTION_REGISTRY:
            with self.subTest(section=entry.section):
                self.assertEqual(
                    entry.config_template, builder.SECTION_OUTPUTS[entry.section]
                )

    def test_registry_runner_prefix_dependencies_and_switches_match_scripts(self):
        from run_machineB_full_validation import SECTION_REGISTRY

        for entry in SECTION_REGISTRY:
            with self.subTest(section=entry.section):
                runner_path = RUNNER_DIR / entry.runner
                self.assertTrue(runner_path.is_file(), runner_path)
                runner_text = runner_path.read_text(encoding="utf-8-sig")
                self.assertIn("common_susi.ps1", runner_text)
                self.assertIn(
                    f"-prefix '{entry.report_prefix}'", runner_text
                )
                for switch in entry.opt_in_switches:
                    self.assertIn(f"[switch]${switch}", runner_text)
                self.assertEqual(entry.script_dependencies, ("common_susi.ps1",))
                self.assertEqual(entry.default_switches, ())

    def test_dangerous_or_fixture_operations_are_explicit_opt_in(self):
        from run_machineB_full_validation import SECTION_REGISTRY_BY_NAME

        expected = {
            "SMBus": ("EnableFixtureTest",),
            "ThermalProtect": ("EnableSetConfigTest",),
            "HWM.Fan": ("EnableStimulus",),
            "VGA.Backlight": ("EnableFunctionalTest",),
            "VGA.Brightness": ("EnableFunctionalTest",),
            "GPIO": ("EnableFunctionalTest",),
            "StorageArea": ("EnableWriteTest",),
            "HWM.Fan.Control": ("AllowControl",),
        }
        actual = {
            section: SECTION_REGISTRY_BY_NAME[section].opt_in_switches
            for section in expected
        }

        self.assertEqual(actual, expected)

    def test_fan_control_immediately_follows_and_depends_on_fan(self):
        from run_machineB_full_validation import SECTION_REGISTRY

        sections = [entry.section for entry in SECTION_REGISTRY]
        fan_index = sections.index("HWM.Fan")
        fan_control = SECTION_REGISTRY[fan_index + 1]

        self.assertEqual(fan_control.section, "HWM.Fan.Control")
        self.assertEqual(fan_control.section_dependencies, ("HWM.Fan",))
        self.assertEqual(
            fan_control.extra_path_arguments,
            ("FanConfigPath", "FanIniPath"),
        )

    def test_execution_tiers_preserve_fan_dependency_group(self):
        from run_machineB_full_validation import ExecutionTier, SECTION_REGISTRY

        tiers = [entry.execution_tier for entry in SECTION_REGISTRY]

        self.assertEqual(tiers[:4], [ExecutionTier.SIMPLE_READ] * 4)
        self.assertEqual(tiers[4:9], [ExecutionTier.GATED_READ] * 5)
        self.assertEqual(tiers[9], ExecutionTier.DEPENDENT_CONTROL)
        self.assertEqual(tiers[10:14], [ExecutionTier.REVERSIBLE_WRITE] * 4)


if __name__ == "__main__":
    unittest.main()
