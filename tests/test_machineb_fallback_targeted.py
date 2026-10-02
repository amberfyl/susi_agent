import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from tests.test_machineb_full_validation_pipeline import (
    FakePipelineTransport,
    make_contract,
    section_plan,
)


class FallbackTransport(FakePipelineTransport):
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.remote_bytes = {}

    def upload(self, local_path, remote_path):
        self.calls.append(("upload", str(local_path), remote_path))
        data = Path(local_path).read_bytes()
        self.remote_bytes[remote_path] = data
        self.remote_files.add(remote_path)

    def remote_sha256(self, remote_path):
        self.calls.append(("sha256", remote_path))
        return hashlib.sha256(self.remote_bytes[remote_path]).hexdigest()


class TargetedFallbackAttemptTests(unittest.TestCase):
    def _candidate(self, text):
        from machineb_fallback import FallbackAttemptCandidate

        return FallbackAttemptCandidate(
            index=1,
            section="HWM.Temperature",
            route_value="0x4E",
            ini_text=text,
            ini_sha256=hashlib.sha256(text.encode("utf-8")).hexdigest(),
            changed_keys=("TCPU", "TSYS"),
            before_routes={"TCPU": "0x2E", "TSYS": "0x2E"},
        )

    def test_candidate_deploy_reload_and_targeted_runner_produce_attempt_evidence(self):
        from run_machineB_full_validation import run_fallback_attempt

        with tempfile.TemporaryDirectory() as td:
            contract = make_contract(td)
            transport = FallbackTransport()
            transport.remote_files.add(contract.target.reload_bat)
            transport.runner_results["HWM.Temperature"] = {
                "category": "HWM.Temperature",
                "result": "FAIL_API",
                "reason": "partial recovery",
                "validation_layers": {
                    "L1_configuration": "PASS",
                    "L2_capability": "FAIL",
                    "L3_api": "FAIL",
                    "L4_readback": "FAIL",
                },
                "result_breakdown": {
                    "pass": ["channel:TCPU"],
                    "pending": [],
                    "fail": ["channel:TSYS"],
                    "na": [],
                },
            }
            manifest = {
                "sections": [
                    section_plan(
                        "HWM.Temperature",
                        "hwm_temperature",
                        "run_hwm_temperature_validation.ps1",
                        "temperature.json",
                    ),
                    section_plan("GPIO", "gpio", "gpio.ps1", "gpio.json"),
                ]
            }
            candidate = self._candidate(
                "[HWM.Temperature]\nTCPU=1,0,0x4E,0x80000001,0,\nTSYS=1,1,0x4E,0x80000001,0,\n"
            )

            evidence = run_fallback_attempt(
                contract,
                manifest,
                transport,
                candidate,
                timeout_seconds=30,
            )

            self.assertEqual(evidence["deploy"]["status"], "PASS")
            self.assertEqual(evidence["reload"]["status"], "PASS")
            self.assertTrue(evidence["reload"]["readiness"]["ready"])
            self.assertEqual(evidence["report"]["category"], "HWM.Temperature")
            self.assertEqual(evidence["report_sha256"], hashlib.sha256(Path(evidence["report_path"]).read_bytes()).hexdigest())
            runners = [call for call in transport.calls if call[0] == "runner"]
            self.assertEqual(len(runners), 1)
            self.assertIn("fallback\\hwm_temperature\\attempt-001\\reports", runners[0][2]["OutDir"])
            self.assertEqual(runners[0][2]["IniPath"], contract.target.runtime_ini)
            self.assertEqual([call[0] for call in transport.calls].count("batch"), 1)

    def test_reload_failure_returns_infrastructure_evidence_without_running_section(self):
        from run_machineB_full_validation import run_fallback_attempt

        with tempfile.TemporaryDirectory() as td:
            contract = make_contract(td)
            transport = FallbackTransport(reload_exit=1)
            transport.remote_files.add(contract.target.reload_bat)
            manifest = {
                "sections": [
                    section_plan(
                        "HWM.Temperature",
                        "hwm_temperature",
                        "run_hwm_temperature_validation.ps1",
                        "temperature.json",
                    )
                ]
            }
            candidate = self._candidate(
                "[HWM.Temperature]\nTCPU=1,0,0x4E,0x80000001,0,\n"
            )

            evidence = run_fallback_attempt(
                contract,
                manifest,
                transport,
                candidate,
            )

            self.assertEqual(evidence["deploy"]["status"], "PASS")
            self.assertEqual(evidence["reload"]["status"], "FAIL")
            self.assertIsNone(evidence["report"])
            self.assertFalse(any(call[0] == "runner" for call in transport.calls))

    def test_unknown_or_dependent_section_is_rejected_before_remote_action(self):
        from run_machineB_full_validation import RuntimePhaseError, run_fallback_attempt

        with tempfile.TemporaryDirectory() as td:
            contract = make_contract(td)
            transport = FallbackTransport()
            candidate = self._candidate("[HWM.Temperature]\nTCPU=1,0,0x4E,0x80000001,0,\n")
            manifest = {"sections": []}

            with self.assertRaisesRegex(RuntimePhaseError, "exactly one manifest entry"):
                run_fallback_attempt(contract, manifest, transport, candidate)
            self.assertEqual(transport.calls, [])


class FallbackConvergenceSessionTests(unittest.TestCase):
    def test_session_runs_attempt_writes_evidence_and_always_recovers_runtime(self):
        from run_machineB_full_validation import run_fallback_convergence_session

        baseline = (
            "[HWM.Temperature]\n"
            "TCPU=1,0,0x2E,0x80000001,0,\n"
            "TSYS=1,1,0x2E,0x80000001,0,\n"
        )
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            contract = make_contract(td)
            full_ini = root / "BOARD-pre.ini"
            full_ini.write_text(baseline, encoding="utf-8")
            contract.inputs = SimpleNamespace(full_ini=full_ini)
            transport = FallbackTransport()
            transport.remote_files.add(contract.target.reload_bat)
            transport.runner_results["HWM.Temperature"] = {
                "category": "HWM.Temperature",
                "result": "PASS",
                "reason": "one API channel recovered",
                "validation_layers": {"L3_api": "PASS"},
                "result_breakdown": {
                    "pass": ["channel:TCPU"],
                    "fail": ["channel:TSYS"],
                },
            }
            manifest = {"sections": [section_plan(
                "HWM.Temperature",
                "hwm_temperature",
                "run_hwm_temperature_validation.ps1",
                "temperature.json",
            )]}
            plan = {"sections": [{
                "section": "HWM.Temperature",
                "status": "PLANNED",
                "route_candidates": ["0x4E", "0"],
                "option_fallback_enabled": False,
                "success_condition": "ANY_CHANNEL_API_SUCCEEDED",
            }]}

            with patch(
                "run_machineB_full_validation.converge_fallback_artifacts",
                return_value={"status": "APPLIED", "sections": ["HWM.Temperature"]},
            ):
                result = run_fallback_convergence_session(
                    contract,
                    manifest,
                    {"backup": {"original_exists": False}},
                    transport,
                    plan,
                    runner_timeout_seconds=30,
                )

            self.assertEqual(result["status"], "CONVERGED")
            self.assertEqual(result["sections"][0]["selected_route"], "0x4E")
            self.assertEqual(result["final_runtime_recovery"]["status"], "PASS")
            self.assertTrue(Path(result["output_path"]).is_file())
            self.assertTrue((contract.outputs.run_root / "fallback-effective-full.ini").is_file())
            self.assertEqual(len([call for call in transport.calls if call[0] == "runner"]), 1)
            self.assertTrue(any(call[0] == "remove" for call in transport.calls))


if __name__ == "__main__":
    unittest.main()
