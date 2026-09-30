import json
import tempfile
import unittest
from pathlib import Path


class MachineBSectionConfigIntegrationTests(unittest.TestCase):
    def _build_contract(self, *, source_text: str = "[SMBus]\nChannel1=1,0,0,0,\n"):
        from run_machineB_full_validation import build_post_ini_contract

        temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(temp_dir.cleanup)
        root = Path(temp_dir.name)
        project = "BOARD"
        case_dir = root / "CASES" / project
        runner_dir = root / "targetB_task" / "machineB_validation"
        case_dir.mkdir(parents=True)
        runner_dir.mkdir(parents=True)

        (case_dir / f"{project}-pre.ini").write_text(
            "[Information]\nPlatformName=BOARD\n" + source_text,
            encoding="utf-8",
        )
        split_ini = case_dir / f"{project}_SMBus.ini"
        split_ini.write_text(source_text, encoding="utf-8")
        (case_dir / f"{project}-section-matrix.json").write_text(
            json.dumps(
                {
                    "project": project,
                    "sections": [
                        {
                            "section": "SMBus",
                            "status": "GENERATED",
                            "path": str(split_ini),
                        }
                    ],
                }
            ),
            encoding="utf-8",
        )
        (runner_dir / "run_smbus_validation.ps1").write_text(
            ". ./common_susi.ps1\n", encoding="utf-8"
        )
        (runner_dir / "common_susi.ps1").write_text(
            "# shared helper\n", encoding="utf-8"
        )
        contract = build_post_ini_contract(
            project=project, repo_root=root, run_id="p5-run"
        )
        return root, contract

    def test_callable_builder_materializes_generated_section_config(self):
        from build_machineB_section_configs import build_section_configs

        _, contract = self._build_contract()

        result = build_section_configs(
            matrix_path=contract.inputs.section_matrix,
            output_dir=contract.inputs.config_dir,
            model=contract.project,
            prune_stale=True,
        )

        config_path = contract.inputs.config_dir / "BOARD_smbus.json"
        self.assertTrue(config_path.is_file())
        config = json.loads(config_path.read_text(encoding="utf-8"))
        self.assertEqual(config["category"], "SMBus")
        self.assertEqual(config["model"], "BOARD")
        self.assertEqual(result["status"], "PASS")
        self.assertEqual(result["generated_sections"], ["SMBus"])
        self.assertEqual(result["config_paths"], {"SMBus": str(config_path)})

    def test_prepare_configs_prunes_stale_known_section_outputs(self):
        from run_machineB_full_validation import prepare_section_configs

        _, contract = self._build_contract()
        contract.inputs.config_dir.mkdir()
        stale_path = contract.inputs.config_dir / "BOARD_gpio.json"
        stale_path.write_text("{}\n", encoding="utf-8")

        result = prepare_section_configs(contract)

        self.assertFalse(stale_path.exists())
        self.assertEqual(result["status"], "PASS")
        self.assertEqual(result["section_count"], 1)
        self.assertEqual(result["generated_sections"], ["SMBus"])
        self.assertEqual(result["removed_stale_configs"], [str(stale_path)])

    def test_dry_run_builds_configs_before_preflight_and_records_build(self):
        from run_machineB_full_validation import write_dry_run_manifest

        _, contract = self._build_contract()
        config_path = contract.inputs.config_dir / "BOARD_smbus.json"
        self.assertFalse(config_path.exists())

        manifest_path = write_dry_run_manifest(contract)

        self.assertTrue(config_path.is_file())
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        self.assertEqual(manifest["config_build"]["status"], "PASS")
        self.assertEqual(manifest["config_build"]["section_count"], 1)
        self.assertEqual(manifest["preflight"]["status"], "PASS")

    def test_manifest_schema_requires_config_build_metadata(self):
        from run_machineB_full_validation import EXECUTION_MANIFEST_SCHEMA

        self.assertIn("config_build", EXECUTION_MANIFEST_SCHEMA["required"])
        self.assertEqual(
            EXECUTION_MANIFEST_SCHEMA["properties"]["config_build"]["type"],
            "object",
        )

    def test_builder_failure_has_fixed_contract_error_and_no_manifest(self):
        from run_machineB_full_validation import (
            ContractError,
            ContractErrorCode,
            write_dry_run_manifest,
        )

        _, contract = self._build_contract(source_text="[SMBus]\nChannel1=bad\n")

        with self.assertRaises(ContractError) as caught:
            write_dry_run_manifest(contract)

        self.assertEqual(caught.exception.code, ContractErrorCode.CONFIG_BUILD_FAILED)
        self.assertEqual(caught.exception.path, contract.inputs.section_matrix)
        self.assertFalse(contract.outputs.manifest.exists())
        self.assertFalse(
            (contract.inputs.config_dir / "BOARD_smbus.json").exists()
        )


if __name__ == "__main__":
    unittest.main()
