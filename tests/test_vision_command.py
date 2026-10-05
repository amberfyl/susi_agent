import importlib.util
import os
import subprocess
import unittest
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
GENERATOR_PATH = ROOT / "susi_gen.py"


def load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


generator = load_module("susi_generator_vision_command", GENERATOR_PATH)


class VisionCommandTests(unittest.TestCase):
    def test_default_is_hermes(self):
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("SUSI_VISION_CMD", None)
            cmd = generator._vision_command("look at it", Path("bios1.png"))
        self.assertEqual(cmd, ["hermes", "-z", "look at it", "-t", "vision"])

    def test_custom_agent_template_with_image_placeholder(self):
        env = {"SUSI_VISION_CMD": "my-agent run --image {image} {prompt}"}
        with patch.dict(os.environ, env):
            cmd = generator._vision_command("trace the GPIO wires", Path("/p/circuit_1.png"))
        self.assertEqual(cmd, ["my-agent", "run", "--image", "/p/circuit_1.png", "trace the GPIO wires"])

    def test_prompt_with_spaces_and_quotes_stays_one_argument(self):
        env = {"SUSI_VISION_CMD": "agent {prompt}"}
        prompt = 'Output JSON: {"items": []} and "quotes"'
        with patch.dict(os.environ, env):
            cmd = generator._vision_command(prompt, Path("a.png"))
        self.assertEqual(cmd, ["agent", prompt])

    def test_run_vision_analyze_uses_configured_command(self):
        env = {"SUSI_VISION_CMD": "other-agent --img {image} {prompt}"}
        done = subprocess.CompletedProcess(args=[], returncode=0, stdout=" result \n", stderr="")
        with patch.dict(os.environ, env), patch.object(generator.subprocess, "run", return_value=done) as run:
            out = generator._run_vision_analyze(Path("bios1.png"), "read it")
        self.assertEqual(out, "result")
        self.assertEqual(run.call_args.args[0], ["other-agent", "--img", "bios1.png", "read it"])

    def test_missing_command_returns_none(self):
        with patch.object(generator.subprocess, "run", side_effect=FileNotFoundError("no such agent")):
            self.assertIsNone(generator._run_vision_analyze(Path("bios1.png"), "read it"))


if __name__ == "__main__":
    unittest.main()
