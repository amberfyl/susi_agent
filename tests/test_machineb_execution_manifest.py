import json
import tempfile
import unittest
from pathlib import Path


class ExecutionManifestTests(unittest.TestCase):
    def _build_contract(self, sections):
        from run_machineB_full_validation import build_post_ini_contract

        temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(temp_dir.cleanup)
        root = Path(temp_dir.name)
        project = "BOARD"
        case_dir = root / "CASES" / project
        case_dir.mkdir(parents=True)
        (root / "targetB_task" / "machineB_validation").mkdir(parents=True)
        (case_dir / f"{project}-pre.ini").write_text(
            "[Information]\nPlatformName=BOARD\n", encoding="utf-8"
        )

        matrix_rows = []
        for section, status in sections:
            path = None
            if status == "GENERATED":
                split_ini = case_dir / f"{project}_{section}.ini"
                split_ini.write_text(f"[{section}]\n", encoding="utf-8")
                path = str(split_ini)
            matrix_rows.append(
                {"section": section, "status": status, "path": path}
            )

        (case_dir / f"{project}-section-matrix.json").write_text(
            json.dumps({"project": project, "sections": matrix_rows}),
            encoding="utf-8",
        )
        contract = build_post_ini_contract(
            project=project, repo_root=root, run_id="run-1"
        )
        return contract

    def test_manifest_uses_registry_order_not_matrix_order(self):
        from run_machineB_full_validation import build_execution_manifest

        contract = self._build_contract(
            [
                ("StorageArea", "GENERATED"),
                ("HWM.Fan.Control", "GENERATED"),
                ("HWM.Fan", "GENERATED"),
                ("GPIO", "SKIPPED_EMPTY_SECTION"),
            ]
        )

        manifest = build_execution_manifest(contract)

        self.assertEqual(manifest["schema_version"], "machineb.execution_manifest.v1")
        self.assertEqual(manifest["contract_schema_version"], contract.schema_version)
        self.assertEqual(manifest["project"], "BOARD")
        self.assertEqual(manifest["run_id"], "run-1")
        self.assertEqual(manifest["mode"], "dry-run")
        self.assertEqual(
            [section["section"] for section in manifest["sections"]],
            ["HWM.Fan", "HWM.Fan.Control", "StorageArea"],
        )
        self.assertEqual(
            [section["sequence"] for section in manifest["sections"]], [1, 2, 3]
        )
        json.dumps(manifest)

    def test_manifest_resolves_registry_paths_and_metadata(self):
        from run_machineB_full_validation import build_execution_manifest

        contract = self._build_contract([("SMBus", "GENERATED")])

        section = build_execution_manifest(contract)["sections"][0]

        self.assertEqual(section["section"], "SMBus")
        self.assertEqual(section["matrix_status"], "GENERATED")
        self.assertEqual(section["config_path"], str(contract.inputs.config_dir / "BOARD_smbus.json"))
        self.assertEqual(section["runner_path"], str(contract.inputs.runner_dir / "run_smbus_validation.ps1"))
        self.assertEqual(section["split_ini"], str(contract.inputs.split_inis["SMBus"]))
        self.assertEqual(section["report_prefix"], "smbus")
        self.assertEqual(section["execution_tier"], "gated_read")
        self.assertEqual(section["script_dependencies"], ["common_susi.ps1"])
        self.assertEqual(section["section_dependencies"], [])
        self.assertEqual(section["available_opt_in_switches"], ["EnableFixtureTest"])
        self.assertEqual(section["enabled_switches"], [])

    def test_manifest_rejects_unregistered_generated_section(self):
        from run_machineB_full_validation import (
            ContractError,
            ContractErrorCode,
            build_execution_manifest,
        )

        contract = self._build_contract([("Unknown.Section", "GENERATED")])

        with self.assertRaises(ContractError) as caught:
            build_execution_manifest(contract)

        self.assertEqual(
            caught.exception.code, ContractErrorCode.GENERATED_SECTION_NOT_REGISTERED
        )
        self.assertEqual(caught.exception.section, "Unknown.Section")

    def test_manifest_rejects_missing_generated_dependency(self):
        from run_machineB_full_validation import (
            ContractError,
            ContractErrorCode,
            build_execution_manifest,
        )

        contract = self._build_contract([("HWM.Fan.Control", "GENERATED")])

        with self.assertRaises(ContractError) as caught:
            build_execution_manifest(contract)

        self.assertEqual(
            caught.exception.code, ContractErrorCode.SECTION_DEPENDENCY_MISSING
        )
        self.assertEqual(caught.exception.section, "HWM.Fan.Control")

    def test_manifest_safety_policy_disables_every_opt_in_switch(self):
        from run_machineB_full_validation import SECTION_REGISTRY, build_execution_manifest

        contract = self._build_contract(
            [(entry.section, "GENERATED") for entry in SECTION_REGISTRY]
        )

        manifest = build_execution_manifest(contract)
        expected = sorted(
            {
                switch
                for entry in SECTION_REGISTRY
                for switch in entry.opt_in_switches
            }
        )

        self.assertEqual(manifest["safety_policy"]["default"], "disabled")
        self.assertEqual(manifest["safety_policy"]["enabled_opt_in_switches"], [])
        self.assertEqual(
            manifest["safety_policy"]["disabled_opt_in_switches"], expected
        )
        self.assertTrue(
            all(not section["enabled_switches"] for section in manifest["sections"])
        )
        self.assertFalse(contract.outputs.run_root.exists())


if __name__ == "__main__":
    unittest.main()
