import json
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class PostIniContractTests(unittest.TestCase):
    def _make_case(self, root: Path, project: str = "BOARD") -> Path:
        case_dir = root / "CASES" / project
        case_dir.mkdir(parents=True)
        (case_dir / f"{project}-pre.ini").write_text(
            "[Information]\nPlatformName=BOARD\n[SMBus]\nChannel1=1,0,0,0,\n",
            encoding="utf-8",
        )
        split_ini = case_dir / f"{project}_SMBus.ini"
        split_ini.write_text("[SMBus]\nChannel1=1,0,0,0,\n", encoding="utf-8")
        matrix = {
            "project": project,
            "sections": [
                {
                    "section": "SMBus",
                    "status": "GENERATED",
                    "path": str(split_ini),
                },
                {
                    "section": "GPIO",
                    "status": "SKIPPED_EMPTY_SECTION",
                    "path": None,
                },
            ],
        }
        (case_dir / f"{project}-section-matrix.json").write_text(
            json.dumps(matrix), encoding="utf-8"
        )
        (root / "targetB_task" / "machineB_validation").mkdir(parents=True)
        return case_dir

    def test_build_contract_resolves_canonical_inputs_outputs_and_target_paths(self):
        from run_machineB_full_validation import build_post_ini_contract

        with tempfile.TemporaryDirectory() as td:
            repo_root = Path(td)
            case_dir = self._make_case(repo_root)

            contract = build_post_ini_contract(
                project="BOARD", repo_root=repo_root, run_id="20260929_120000"
            )

            self.assertEqual(contract.schema_version, "machineb.post_ini_contract.v1")
            self.assertEqual(contract.project, "BOARD")
            self.assertEqual(contract.inputs.case_dir, case_dir.resolve())
            self.assertEqual(
                contract.inputs.full_ini, (case_dir / "BOARD-pre.ini").resolve()
            )
            self.assertEqual(
                contract.inputs.section_matrix,
                (case_dir / "BOARD-section-matrix.json").resolve(),
            )
            self.assertEqual(contract.inputs.generated_sections, ("SMBus",))
            self.assertEqual(
                contract.inputs.split_inis["SMBus"],
                (case_dir / "BOARD_SMBus.ini").resolve(),
            )
            self.assertEqual(contract.inputs.config_dir, (case_dir / "config").resolve())
            self.assertEqual(
                contract.inputs.runner_dir,
                (repo_root / "targetB_task" / "machineB_validation").resolve(),
            )

            run_root = (case_dir / "validation_runs" / "20260929_120000").resolve()
            self.assertEqual(contract.outputs.run_root, run_root)
            self.assertEqual(contract.outputs.manifest, run_root / "manifest.json")
            self.assertEqual(contract.outputs.reports_dir, run_root / "reports")
            self.assertEqual(
                contract.outputs.summary_json,
                run_root / "BOARD-machineB-summary.json",
            )
            self.assertEqual(
                contract.outputs.summary_text,
                run_root / "BOARD-machineB-summary.txt",
            )
            self.assertEqual(contract.outputs.reload_log, run_root / "reload.log")
            self.assertEqual(
                contract.outputs.manifest_schema_version,
                "machineb.execution_manifest.v1",
            )
            self.assertEqual(
                contract.outputs.summary_schema_version,
                "machineb.validation_summary.v1",
            )

            self.assertEqual(
                contract.target.workspace,
                r"C:\Users\susiaa\Desktop\verify\BOARD",
            )
            self.assertEqual(
                contract.target.run_output,
                r"C:\Users\susiaa\Desktop\verify\BOARD\out\20260929_120000",
            )
            self.assertEqual(contract.target.runtime_ini, r"C:\Windows\SUSI\BOARD.ini")
            self.assertEqual(
                contract.target.reload_bat,
                r"C:\Users\susiaa\Desktop\reload driver\reload_susi4_driver.bat",
            )

            self.assertFalse(run_root.exists(), "building a contract must not create outputs")

    def test_contract_serializes_paths_and_generated_sections_as_json_values(self):
        from run_machineB_full_validation import build_post_ini_contract

        with tempfile.TemporaryDirectory() as td:
            repo_root = Path(td)
            self._make_case(repo_root)
            contract = build_post_ini_contract(
                project="BOARD", repo_root=repo_root, run_id="run-1"
            )

            payload = contract.to_dict()

            self.assertEqual(payload["schema_version"], "machineb.post_ini_contract.v1")
            self.assertEqual(payload["inputs"]["generated_sections"], ["SMBus"])
            self.assertTrue(payload["inputs"]["full_ini"].endswith("/BOARD-pre.ini"))
            self.assertEqual(
                payload["target"]["runtime_ini"], r"C:\Windows\SUSI\BOARD.ini"
            )
            json.dumps(payload)

    def test_manifest_and_summary_schemas_fix_required_top_level_fields(self):
        from run_machineB_full_validation import (
            EXECUTION_MANIFEST_SCHEMA,
            VALIDATION_SUMMARY_SCHEMA,
        )

        self.assertEqual(
            EXECUTION_MANIFEST_SCHEMA["properties"]["schema_version"]["const"],
            "machineb.execution_manifest.v1",
        )
        self.assertEqual(
            EXECUTION_MANIFEST_SCHEMA["required"],
            [
                "schema_version",
                "contract_schema_version",
                "project",
                "run_id",
                "mode",
                "inputs",
                "target",
                "sections",
                "safety_policy",
                "config_build",
                "staging_plan",
            ],
        )
        self.assertEqual(
            VALIDATION_SUMMARY_SCHEMA["properties"]["schema_version"]["const"],
            "machineb.validation_summary.v1",
        )
        self.assertEqual(
            VALIDATION_SUMMARY_SCHEMA["required"],
            [
                "schema_version",
                "contract_schema_version",
                "project",
                "run_id",
                "status",
                "exit_code",
                "timestamps",
                "runtime_ini",
                "reload",
                "sections",
                "warnings",
                "errors",
            ],
        )

    def test_missing_full_ini_has_fixed_error_code(self):
        from run_machineB_full_validation import (
            ContractError,
            ContractErrorCode,
            build_post_ini_contract,
        )

        with tempfile.TemporaryDirectory() as td:
            repo_root = Path(td)
            case_dir = self._make_case(repo_root)
            (case_dir / "BOARD-pre.ini").unlink()

            with self.assertRaises(ContractError) as caught:
                build_post_ini_contract(
                    project="BOARD", repo_root=repo_root, run_id="run-1"
                )

            self.assertEqual(caught.exception.code, ContractErrorCode.FULL_INI_MISSING)
            self.assertEqual(caught.exception.path, case_dir / "BOARD-pre.ini")

    def test_matrix_project_mismatch_has_fixed_error_code(self):
        from run_machineB_full_validation import (
            ContractError,
            ContractErrorCode,
            build_post_ini_contract,
        )

        with tempfile.TemporaryDirectory() as td:
            repo_root = Path(td)
            case_dir = self._make_case(repo_root)
            matrix_path = case_dir / "BOARD-section-matrix.json"
            matrix = json.loads(matrix_path.read_text(encoding="utf-8"))
            matrix["project"] = "OTHER"
            matrix_path.write_text(json.dumps(matrix), encoding="utf-8")

            with self.assertRaises(ContractError) as caught:
                build_post_ini_contract(
                    project="BOARD", repo_root=repo_root, run_id="run-1"
                )

            self.assertEqual(
                caught.exception.code, ContractErrorCode.MATRIX_PROJECT_MISMATCH
            )

    def test_generated_section_requires_an_existing_split_ini(self):
        from run_machineB_full_validation import (
            ContractError,
            ContractErrorCode,
            build_post_ini_contract,
        )

        with tempfile.TemporaryDirectory() as td:
            repo_root = Path(td)
            case_dir = self._make_case(repo_root)
            (case_dir / "BOARD_SMBus.ini").unlink()

            with self.assertRaises(ContractError) as caught:
                build_post_ini_contract(
                    project="BOARD", repo_root=repo_root, run_id="run-1"
                )

            self.assertEqual(
                caught.exception.code, ContractErrorCode.GENERATED_SPLIT_INI_MISSING
            )
            self.assertEqual(caught.exception.section, "SMBus")

    def test_duplicate_matrix_section_has_fixed_error_code(self):
        from run_machineB_full_validation import (
            ContractError,
            ContractErrorCode,
            build_post_ini_contract,
        )

        with tempfile.TemporaryDirectory() as td:
            repo_root = Path(td)
            case_dir = self._make_case(repo_root)
            matrix_path = case_dir / "BOARD-section-matrix.json"
            matrix = json.loads(matrix_path.read_text(encoding="utf-8"))
            matrix["sections"].append(dict(matrix["sections"][0]))
            matrix_path.write_text(json.dumps(matrix), encoding="utf-8")

            with self.assertRaises(ContractError) as caught:
                build_post_ini_contract(
                    project="BOARD", repo_root=repo_root, run_id="run-1"
                )

            self.assertEqual(
                caught.exception.code, ContractErrorCode.DUPLICATE_MATRIX_SECTION
            )
            self.assertEqual(caught.exception.section, "SMBus")

    def test_project_and_run_id_reject_path_components(self):
        from run_machineB_full_validation import (
            ContractError,
            ContractErrorCode,
            build_post_ini_contract,
        )

        with tempfile.TemporaryDirectory() as td:
            repo_root = Path(td)
            for project, run_id in (("../BOARD", "run-1"), ("BOARD", "../run-1")):
                with self.subTest(project=project, run_id=run_id):
                    with self.assertRaises(ContractError) as caught:
                        build_post_ini_contract(
                            project=project, repo_root=repo_root, run_id=run_id
                        )
                    self.assertEqual(
                        caught.exception.code, ContractErrorCode.INVALID_PATH_TOKEN
                    )


if __name__ == "__main__":
    unittest.main()
