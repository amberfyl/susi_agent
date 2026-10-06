"""Single exit for every LLM call in the SUSI pipeline.

The program never chooses a provider, model or API key. It runs the current
agent CLI, which answers with whatever credentials and model that agent is
configured with (for Hermes: ``model.provider`` / ``model.default`` in
``~/.hermes/config.yaml``, e.g. GitHub Copilot).

Command template (first non-empty wins):
  SUSI_AGENT_CMD   e.g. "hermes -z {prompt} -t vision"
  SUSI_VISION_CMD  legacy name, same meaning
  default          DEFAULT_AGENT_CMD

Placeholders: ``{prompt}`` (required) is the full prompt as one argument;
``{image}`` (optional) is the first image path. All image paths are also listed
in the prompt so the agent can open them with its vision tool. ``-t vision``
limits the agent to the vision toolset, so a one-shot call cannot run commands
or edit files.
"""

from __future__ import annotations

import json
import os
import shlex
import subprocess
import sys
from pathlib import Path

DEFAULT_AGENT_CMD = "hermes -z {prompt} -t vision"
DEFAULT_TIMEOUT_SEC = 900
# Linux limits a single argv string to 128 KiB (MAX_ARG_STRLEN).
MAX_PROMPT_BYTES = 120 * 1024


class AgentLLMError(RuntimeError):
    """The agent CLI could not produce a usable answer."""


def agent_command_template() -> str:
    for name in ("SUSI_AGENT_CMD", "SUSI_VISION_CMD"):
        value = os.environ.get(name, "").strip()
        if value:
            return value
    return DEFAULT_AGENT_CMD


def agent_command(prompt: str, image_path: Path | None = None) -> list[str]:
    """Build argv from the template. The template is split first so the prompt
    always stays a single argument, whatever quotes or spaces it contains."""

    image = str(image_path) if image_path is not None else ""
    return [
        part.replace("{prompt}", prompt).replace("{image}", image)
        for part in shlex.split(agent_command_template())
    ]


def with_image_list(prompt: str, image_paths: list[Path]) -> str:
    if not image_paths:
        return prompt
    listed = "\n".join(f"- {Path(p).resolve()}" for p in image_paths)
    return (
        f"{prompt}\n\n## Attached images\n"
        f"Open each of these files with your vision tool before answering:\n{listed}"
    )


def run_agent(
    prompt: str,
    image_path: Path | None = None,
    *,
    timeout: int = DEFAULT_TIMEOUT_SEC,
) -> str | None:
    """Run the agent once and return its stripped stdout, or None on failure."""

    cmd = agent_command(prompt, image_path)
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            encoding="utf-8",
            errors="replace",
            check=False,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        print(f"WARNING: agent command timed out after {timeout}s ({cmd[0]})", file=sys.stderr)
        return None
    except Exception as exc:
        print(f"WARNING: agent command failed to start ({cmd[0]}): {exc}", file=sys.stderr)
        return None

    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "").strip().splitlines()
        tail = detail[-1] if detail else ""
        print(f"WARNING: agent command exited {proc.returncode} ({cmd[0]}): {tail}", file=sys.stderr)
        return None

    out = (proc.stdout or "").strip()
    return out or None


def parse_json_object(text: str) -> dict:
    """Parse the JSON object in an agent reply (tolerates ``` fences / prose)."""

    stripped = text.strip()
    if stripped.startswith("```"):
        stripped = stripped.split("\n", 1)[1] if "\n" in stripped else ""
        if stripped.rstrip().endswith("```"):
            stripped = stripped.rstrip()[:-3]
    try:
        value = json.loads(stripped)
    except json.JSONDecodeError:
        start, end = stripped.find("{"), stripped.rfind("}")
        if start < 0 or end <= start:
            raise
        value = json.loads(stripped[start:end + 1])
    if not isinstance(value, dict):
        raise json.JSONDecodeError("reply is not a JSON object", stripped, 0)
    return value


def ask_agent_for_json(
    instructions: str,
    user_message: str,
    image_paths: list[Path] | None = None,
    *,
    purpose: str,
    timeout: int = DEFAULT_TIMEOUT_SEC,
) -> dict:
    """Send instructions + message (+ image paths) and return the JSON object."""

    images = list(image_paths or [])
    prompt = with_image_list(
        f"{instructions.strip()}\n\n"
        "Reply with exactly one JSON object and nothing else.\n\n"
        f"{user_message}",
        images,
    )
    size = len(prompt.encode("utf-8"))
    if size > MAX_PROMPT_BYTES:
        raise AgentLLMError(
            f"{purpose}: prompt is {size} bytes, over the {MAX_PROMPT_BYTES}-byte argv limit"
        )

    template = agent_command_template()
    print(
        f"Calling agent for {purpose} ({len(images)} image(s)) via `{template}` "
        f"— provider/model come from the agent's current settings …",
        file=sys.stderr,
    )
    reply = run_agent(prompt, images[0] if images else None, timeout=timeout)
    if reply is None:
        raise AgentLLMError(f"{purpose}: agent returned no answer (see warning above)")
    try:
        return parse_json_object(reply)
    except json.JSONDecodeError as exc:
        raise AgentLLMError(
            f"{purpose}: agent reply is not a JSON object: {exc}\n\nReply:\n{reply[:500]}"
        ) from exc
