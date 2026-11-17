import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from test_machineb_full_validation_pipeline import (  # noqa: E402
    FakePipelineTransport,
    make_contract,
    section_plan,
)


class BoardInfoRegistryTests(unittest.TestCase):
    def test_board_info_is_a_standalone_section_that_runs_first(self):
        from run_machineB_full_validation import SECTION_REGISTRY

        entry = SECTION_REGISTRY[0]
        self.assertEqual(entry.section, "BoardInfo")
        self.assertTrue(entry.standalone)
        self.assertEqual(entry.runner, "run_boardinfo_validation.ps1")
        self.assertEqual(entry.report_prefix, "boardinfo")
        self.assertEqual(entry.opt_in_switches, ())
        self.assertEqual(sum(1 for item in SECTION_REGISTRY if item.standalone), 1)
        self.assertEqual(len(SECTION_REGISTRY), 15)

    def test_only_ini_sections_have_a_generated_config(self):
        import build_machineB_section_configs as builder
        from run_machineB_full_validation import SECTION_REGISTRY

        ini_sections = {entry.section for entry in SECTION_REGISTRY if not entry.standalone}
        self.assertEqual(ini_sections, set(builder.SECTION_OUTPUTS))


class BoardInfoManifestTests(unittest.TestCase):
    def _contract(self, sections):
        from run_machineB_full_validation import build_post_ini_contract

        temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(temp_dir.cleanup)
        root = Path(temp_dir.name)
        project = "BOARD"
        case_dir = root / "CASES" / project
        case_dir.mkdir(parents=True)
        (root / "targetB_task" / "machineB_validation").mkdir(parents=True)
        (case_dir / f"{project}-pre.ini").write_text("[Information]\nPlatformName=BOARD\n", encoding="utf-8")
        rows = []
        for section in sections:
            split_ini = case_dir / f"{project}_{section}.ini"
            split_ini.write_text(f"[{section}]\n", encoding="utf-8")
            rows.append({"section": section, "status": "GENERATED", "path": str(split_ini)})
        (case_dir / f"{project}-section-matrix.json").write_text(
            json.dumps({"project": project, "sections": rows}), encoding="utf-8"
        )
        return build_post_ini_contract(project=project, repo_root=root, run_id="run-1")

    def test_manifest_always_plans_board_info_first_without_ini_or_config(self):
        from run_machineB_full_validation import build_execution_manifest

        manifest = build_execution_manifest(self._contract(["GPIO"]))
        self.assertEqual([s["section"] for s in manifest["sections"]], ["BoardInfo", "GPIO"])
        board = manifest["sections"][0]
        self.assertIsNone(board["split_ini"])
        self.assertIsNone(board["config_path"])
        self.assertTrue(board["runner_path"].endswith("run_boardinfo_validation.ps1"))
        self.assertEqual([s["sequence"] for s in manifest["sections"]], [1, 2])

    def test_staging_plan_uploads_runner_but_no_ini_or_config_for_board_info(self):
        from run_machineB_full_validation import build_execution_manifest

        manifest = build_execution_manifest(self._contract(["GPIO"]))
        artifacts = manifest["staging_plan"]["artifacts"]
        board = [a for a in artifacts if a.get("section") == "BoardInfo"]
        kinds = {a["kind"] for a in board}
        self.assertIn("runner", kinds)
        self.assertNotIn("split_ini", kinds)
        self.assertNotIn("config", kinds)

    def test_preflight_accepts_a_section_without_config(self):
        from run_machineB_full_validation import run_local_preflight

        contract = self._contract(["GPIO"])
        runner_dir = contract.inputs.runner_dir
        runner_dir.mkdir(parents=True, exist_ok=True)
        (runner_dir / "run_boardinfo_validation.ps1").write_text("", encoding="utf-8")
        (runner_dir / "common_susi.ps1").write_text("", encoding="utf-8")
        manifest = {
            "sections": [
                {
                    "section": "BoardInfo",
                    "config_path": None,
                    "runner_path": str(runner_dir / "run_boardinfo_validation.ps1"),
                    "script_dependencies": ["common_susi.ps1"],
                }
            ]
        }
        result = run_local_preflight(contract, manifest)
        self.assertEqual(result["status"], "PASS")
        self.assertEqual(result["config_count"], 0)


class BoardInfoExecutionTests(unittest.TestCase):
    def _plan(self):
        return {
            "sequence": 1,
            "section": "BoardInfo",
            "runner_path": "run_boardinfo_validation.ps1",
            "config_path": None,
            "split_ini": None,
            "report_prefix": "boardinfo",
            "section_dependencies": [],
            "available_opt_in_switches": [],
            "enabled_switches": [],
            "extra_path_arguments": [],
        }

    def test_runner_gets_only_the_report_directory(self):
        from run_machineB_full_validation import execute_validation_sections

        with tempfile.TemporaryDirectory() as td:
            contract = make_contract(td)
            transport = FakePipelineTransport()
            results = execute_validation_sections(contract, {"sections": [self._plan()]}, transport)
        self.assertEqual(results[0]["execution_status"], "COMPLETED")
        invocation = [call for call in transport.calls if call[0] == "runner"][0]
        self.assertEqual(list(invocation[2]), ["OutDir"])

    def test_failed_board_info_fails_the_section_with_its_reason(self):
        from run_machineB_full_validation import execute_validation_sections

        with tempfile.TemporaryDirectory() as td:
            contract = make_contract(td)
            transport = FakePipelineTransport()
            transport.runner_results["BoardInfo"] = {
                "category": "BoardInfo",
                "result": "FAIL_READBACK",
                "reason": "Board info value problem: Boot Counter: value is 0 (expected >= 1)",
                "validation_layers": {
                    "L1_configuration": "PASS", "L2_capability": "PASS", "L3_api": "PASS",
                    "L4_readback": "FAIL", "L5_functional": "N_A", "L6_recovery": "N_A",
                },
            }
            results = execute_validation_sections(contract, {"sections": [self._plan()]}, transport)
        self.assertEqual(results[0]["verdict"], "FAIL")
        self.assertIn("value is 0", results[0]["reason"])


if __name__ == "__main__":
    unittest.main()
