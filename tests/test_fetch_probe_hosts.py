import subprocess
import unittest
from unittest.mock import patch

import fetch_probe


class WinrmHostRangeTests(unittest.TestCase):
    def test_allowed_range_is_192_168_100_10_to_50(self):
        allowed = fetch_probe._is_winrm_allowed_host
        self.assertTrue(allowed("192.168.100.10"))
        self.assertTrue(allowed("192.168.100.20"))
        self.assertTrue(allowed("192.168.100.50"))
        self.assertFalse(allowed("192.168.100.9"))
        self.assertFalse(allowed("192.168.100.51"))
        self.assertFalse(allowed("172.22.12.243"))
        self.assertFalse(allowed("not-an-ip"))

    def test_winrm_uses_only_explicit_hosts_in_range(self):
        select = fetch_probe._winrm_hosts
        self.assertEqual(select(["192.168.100.20", "172.22.12.243"]), ["192.168.100.20"])
        # No explicit host: WinRM never guesses from the default pool.
        self.assertEqual(select([]), [])

    def test_explicit_host_comes_first(self):
        hosts = fetch_probe._explicit_hosts("192.168.100.20", "172.22.12.243")
        self.assertEqual(hosts, ["192.168.100.20", "172.22.12.243"])

    def test_no_machine_b_address_is_hardcoded(self):
        self.assertFalse(hasattr(fetch_probe, "DEFAULT_HOST_POOL"))
        self.assertEqual(fetch_probe._explicit_hosts(None, None), [])

    def test_missing_host_is_an_error(self):
        argv = ["fetch_probe.py", "--mode", "ssh", "--ssh-user", "susiaa", "--project", "X"]
        with patch.object(fetch_probe.sys, "argv", argv), \
                patch.object(fetch_probe, "fetch_probe_ssh") as ssh:
            with self.assertRaises(SystemExit) as ctx:
                fetch_probe.main()
        self.assertEqual(ctx.exception.code, 2)
        ssh.assert_not_called()


class AutoModeOrderTests(unittest.TestCase):
    def test_auto_tries_ssh_first_then_winrm(self):
        calls = []

        def fake_ssh(**kwargs):
            calls.append(("ssh", kwargs["host"]))
            raise RuntimeError("ssh down")

        def fake_winrm(**kwargs):
            calls.append(("winrm", kwargs["host"]))

        argv = [
            "fetch_probe.py", "--mode", "auto", "--ssh-user", "susiaa",
            "--host", "172.22.12.243", "--hosts", "192.168.100.20",
            "--project", "X", "--dir", "/nonexistent-not-written",
        ]
        with patch.object(fetch_probe.sys, "argv", argv), \
                patch.object(fetch_probe, "fetch_probe_ssh", side_effect=fake_ssh), \
                patch.object(fetch_probe, "_winrm_preflight", return_value=True), \
                patch.object(fetch_probe, "fetch_probe_winrm", side_effect=fake_winrm):
            fetch_probe.main()

        self.assertEqual(
            calls,
            [("ssh", "172.22.12.243"), ("ssh", "192.168.100.20"), ("winrm", "192.168.100.20")],
        )


class PowershellDecodingTests(unittest.TestCase):
    def test_powershell_output_is_decoded_as_cp950_without_crashing(self):
        # Windows PowerShell on a zh-TW host prints errors in Big5 (cp950).
        big5_error = "找不到主機".encode("cp950")

        def fake_run(cmd, **kwargs):
            self.assertEqual(kwargs.get("encoding"), "cp950")
            self.assertEqual(kwargs.get("errors"), "replace")
            return subprocess.CompletedProcess(
                cmd, 1, stdout="", stderr=big5_error.decode(kwargs["encoding"], kwargs["errors"])
            )

        with patch.object(fetch_probe.subprocess, "run", side_effect=fake_run):
            with self.assertRaises(RuntimeError) as ctx:
                fetch_probe._run_powershell("Test-WSMan -ComputerName '192.168.100.16'")
        self.assertIn("找不到主機", str(ctx.exception))

    def test_preflight_failure_returns_false_instead_of_raising(self):
        def fake_run(cmd, **kwargs):
            return subprocess.CompletedProcess(cmd, 1, stdout="", stderr="無法連線")

        with patch.object(fetch_probe.subprocess, "run", side_effect=fake_run):
            self.assertFalse(fetch_probe._winrm_preflight("192.168.100.16", None, None))


if __name__ == "__main__":
    unittest.main()
