import json
import tempfile
import unittest
from pathlib import Path, PureWindowsPath


def _contract_and_manifest(testcase, *, with_reload_files=True):
    from run_machineB_full_validation import (
        build_execution_manifest,
        build_post_ini_contract,
        prepare_section_configs,
    )

    temp_dir = tempfile.TemporaryDirectory()
    testcase.addCleanup(temp_dir.cleanup)
    root = Path(temp_dir.name)
    project = "BOARD"
    case_dir = root / "CASES" / project
    runner_dir = root / "targetB_task" / "machineB_validation"
    case_dir.mkdir(parents=True)
    runner_dir.mkdir(parents=True)
    source_text = "[SMBus]\nChannel1=1,0,0,0,\n"
    (case_dir / f"{project}-pre.ini").write_text(
        "[Information]\nPlatformName=BOARD\n" + source_text, encoding="utf-8"
    )
    split_ini = case_dir / f"{project}_SMBus.ini"
    split_ini.write_text(source_text, encoding="utf-8")
    (case_dir / f"{project}-section-matrix.json").write_text(
        json.dumps({"project": project, "sections": [
            {"section": "SMBus", "status": "GENERATED", "path": str(split_ini)}
        ]}),
        encoding="utf-8",
    )
    for name in ("run_smbus_validation.ps1", "run_boardinfo_validation.ps1", "common_susi.ps1"):
        (runner_dir / name).write_text("# stub\n", encoding="utf-8")
    if with_reload_files:
        (runner_dir / "reload_susi4_driver.bat").write_text("@echo off\n", encoding="utf-8")
        (runner_dir / "reload_susi4_driver.ps1").write_text("# reload\n", encoding="utf-8")
    contract = build_post_ini_contract(project=project, repo_root=root, run_id="p6-run")
    prepare_section_configs(contract)
    return contract, build_execution_manifest(contract)


class ReloadScriptStagingTests(unittest.TestCase):
    def test_plan_uploads_both_reload_files_next_to_the_reload_bat(self):
        from run_machineB_full_validation import build_remote_staging_plan

        contract, manifest = _contract_and_manifest(self)
        plan = build_remote_staging_plan(contract, manifest)
        reload_dir = PureWindowsPath(contract.target.reload_bat).parent
        remote = {
            PureWindowsPath(a["remote_path"]).name: a
            for a in plan["artifacts"] if a["kind"] == "reload_script"
        }
        self.assertEqual(sorted(remote), ["reload_susi4_driver.bat", "reload_susi4_driver.ps1"])
        for name, artifact in remote.items():
            self.assertEqual(PureWindowsPath(artifact["remote_path"]), reload_dir / name)
            self.assertEqual(Path(artifact["local_path"]).name, name)
        self.assertIn(str(reload_dir), plan["directories"])

    def test_remote_bat_path_is_one_of_the_staged_files(self):
        from run_machineB_full_validation import build_remote_staging_plan

        contract, manifest = _contract_and_manifest(self)
        plan = build_remote_staging_plan(contract, manifest)
        staged = {str(PureWindowsPath(a["remote_path"])).casefold() for a in plan["artifacts"]}
        self.assertIn(str(PureWindowsPath(contract.target.reload_bat)).casefold(), staged)

    def test_preflight_fails_when_a_reload_file_is_missing(self):
        from run_machineB_full_validation import ContractError, run_local_preflight

        contract, manifest = _contract_and_manifest(self, with_reload_files=False)
        with self.assertRaises(ContractError) as ctx:
            run_local_preflight(contract, manifest)
        self.assertIn("reload", str(ctx.exception))

    def test_preflight_passes_when_reload_files_exist(self):
        from run_machineB_full_validation import run_local_preflight

        contract, manifest = _contract_and_manifest(self)
        self.assertEqual(run_local_preflight(contract, manifest)["status"], "PASS")


if __name__ == "__main__":
    unittest.main()
