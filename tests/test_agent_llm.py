import os
import subprocess
import unittest
from pathlib import Path
from unittest.mock import patch

import agent_llm

ROOT = Path(__file__).resolve().parents[1]


def clean_env(**values):
    env = {k: v for k, v in os.environ.items() if k not in ("SUSI_AGENT_CMD", "SUSI_VISION_CMD")}
    env.update(values)
    return patch.dict(os.environ, env, clear=True)


class AgentCommandTests(unittest.TestCase):
    def test_default_is_hermes_oneshot_limited_to_vision_toolset(self):
        with clean_env():
            self.assertEqual(agent_llm.agent_command("hi"), ["hermes", "-z", "hi", "-t", "vision"])

    def test_agent_cmd_wins_over_legacy_vision_cmd(self):
        with clean_env(SUSI_AGENT_CMD="new-agent {prompt}", SUSI_VISION_CMD="old-agent {prompt}"):
            self.assertEqual(agent_llm.agent_command("hi"), ["new-agent", "hi"])
        with clean_env(SUSI_VISION_CMD="old-agent {prompt}"):
            self.assertEqual(agent_llm.agent_command("hi"), ["old-agent", "hi"])

    def test_image_paths_are_listed_in_the_prompt(self):
        prompt = agent_llm.with_image_list("analyse", [Path("/case/a.png"), Path("/case/b.jpg")])
        self.assertIn("## Attached images", prompt)
        self.assertIn("- /case/a.png", prompt)
        self.assertIn("- /case/b.jpg", prompt)
        self.assertEqual(agent_llm.with_image_list("analyse", []), "analyse")


class ParseJsonTests(unittest.TestCase):
    def test_plain_fenced_and_wrapped_replies(self):
        self.assertEqual(agent_llm.parse_json_object('{"a": 1}'), {"a": 1})
        self.assertEqual(agent_llm.parse_json_object('```json\n{"a": 1}\n```'), {"a": 1})
        self.assertEqual(agent_llm.parse_json_object('Here it is:\n{"a": {"b": 2}}\nDone.'), {"a": {"b": 2}})

    def test_non_object_is_rejected(self):
        with self.assertRaises(ValueError):
            agent_llm.parse_json_object("[1, 2]")


class AskAgentTests(unittest.TestCase):
    def test_returns_parsed_object_and_sends_one_prompt_argument(self):
        done = subprocess.CompletedProcess([], 0, stdout='```json\n{"ok": true}\n```', stderr="")
        with clean_env(), patch.object(agent_llm.subprocess, "run", return_value=done) as run:
            result = agent_llm.ask_agent_for_json("RULES", "FORM TEXT", [], purpose="test")
        self.assertEqual(result, {"ok": True})
        argv = run.call_args.args[0]
        self.assertEqual(argv[:2], ["hermes", "-z"])
        self.assertIn("RULES", argv[2])
        self.assertIn("FORM TEXT", argv[2])
        self.assertEqual(run.call_args.kwargs["timeout"], agent_llm.DEFAULT_TIMEOUT_SEC)

    def test_failures_raise_agent_error(self):
        failed = subprocess.CompletedProcess([], 1, stdout="", stderr="auth expired")
        with clean_env(), patch.object(agent_llm.subprocess, "run", return_value=failed):
            with self.assertRaises(agent_llm.AgentLLMError):
                agent_llm.ask_agent_for_json("R", "M", [], purpose="test")
        not_json = subprocess.CompletedProcess([], 0, stdout="sorry, I cannot", stderr="")
        with clean_env(), patch.object(agent_llm.subprocess, "run", return_value=not_json):
            with self.assertRaises(agent_llm.AgentLLMError):
                agent_llm.ask_agent_for_json("R", "M", [], purpose="test")

    def test_timeout_raises_agent_error(self):
        with clean_env(), patch.object(
            agent_llm.subprocess, "run", side_effect=subprocess.TimeoutExpired("hermes", 900)
        ):
            with self.assertRaises(agent_llm.AgentLLMError):
                agent_llm.ask_agent_for_json("R", "M", [], purpose="test")

    def test_oversized_prompt_is_rejected_before_running(self):
        with clean_env(), patch.object(agent_llm.subprocess, "run") as run:
            with self.assertRaises(agent_llm.AgentLLMError):
                agent_llm.ask_agent_for_json("R", "x" * (agent_llm.MAX_PROMPT_BYTES + 1), [], purpose="test")
        run.assert_not_called()


class NoHardcodedProviderTests(unittest.TestCase):
    def test_pipeline_has_no_direct_provider_client(self):
        for name in ("extract_pdf.py", "understand.py", "susi_gen.py"):
            text = (ROOT / name).read_text(encoding="utf-8")
            with self.subTest(file=name):
                self.assertNotIn("OpenAI(", text)
                self.assertNotIn("LLM_API_KEY", text)
                self.assertNotIn("LLM_MODEL", text)
                self.assertNotIn("gpt-5.3-codex", text)


if __name__ == "__main__":
    unittest.main()
