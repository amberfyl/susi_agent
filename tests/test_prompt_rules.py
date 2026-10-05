import importlib.util
import json
import os
import tempfile
import time
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


generator = load_module("susi_generator_prompt_rules", GENERATOR_PATH)


class LoadPromptRulesTests(unittest.TestCase):
    def test_repository_prompt_files_load(self):
        for name in (generator.BIOS_READING_PROMPT, generator.GPIO_TRACE_PROMPT):
            rules = generator._load_prompt_rules(name)
            self.assertIsNone(rules["issue"], name)
            self.assertTrue(rules["text"])

    def test_missing_file_reports_issue(self):
        with tempfile.TemporaryDirectory() as td:
            rules = generator._load_prompt_rules("gpio_trace.md", Path(td))
        self.assertIsNone(rules["text"])
        self.assertEqual(rules["issue"], "PROMPT_FILE_MISSING: prompts/gpio_trace.md")

    def test_empty_file_reports_issue(self):
        with tempfile.TemporaryDirectory() as td:
            (Path(td) / "gpio_trace.md").write_text("  \n", encoding="utf-8")
            rules = generator._load_prompt_rules("gpio_trace.md", Path(td))
        self.assertEqual(rules["issue"], "PROMPT_FILE_EMPTY: prompts/gpio_trace.md")


class PromptAssemblyTests(unittest.TestCase):
    def test_gpio_prompt_carries_rules_case_info_and_output_contract(self):
        prompt = generator._build_gpio_trace_prompt(
            "RULES-TEXT", "NCT6694B", "EC_P*_GPIO*", Path("circuit_p1.png")
        )
        self.assertIn("RULES-TEXT", prompt)
        self.assertIn("NCT6694B", prompt)
        self.assertIn("EC_P*_GPIO*", prompt)
        self.assertIn("circuit_p1.png", prompt)
        self.assertIn('"group":11', prompt)

    def test_bios_prompt_keeps_parser_output_tokens(self):
        prompt = generator._build_bios_reading_prompt("RULES-TEXT", Path("bios1.png"))
        self.assertIn("RULES-TEXT", prompt)
        for token in ("VOLTAGE_VALUE:", "TEMP_VALUE:", "FAN_VALUE:"):
            self.assertIn(token, prompt)


def _write_cache(project: Path, image: Path, status: str) -> Path:
    cache = project / "P-bios-image-cache.json"
    cache.write_text(json.dumps({
        "items": [{
            "path": str(image),
            "filename": image.name,
            "analysis_status": status,
            "analysis_text": "CPU Temperature 50 C" if status == "DONE_VISION_ANALYZE" else "",
            "voltage_label_hints": ["+12V"],
            "voltage_value_hints": [{"label": "+12V", "value": 12.0, "unit": "V"}],
            "temperature_value_hints": [{"label": "CPU TEMPERATURE", "value": 50.0, "unit": "C"}],
            "fan_value_hints": [],
        }],
    }), encoding="utf-8")
    return cache


class BiosCacheReuseTests(unittest.TestCase):
    """Results written beforehand (e.g. by the Hermes agent) must be used as-is."""

    def setUp(self):
        self.td = tempfile.TemporaryDirectory()
        self.project = Path(self.td.name)
        self.prompts = self.project / "prompts"
        self.prompts.mkdir()
        (self.prompts / "bios_reading.md").write_text("bios rules", encoding="utf-8")
        self.image = self.project / "bios1.png"
        self.image.write_bytes(b"png")

    def tearDown(self):
        self.td.cleanup()

    def test_analyzed_item_is_not_reanalyzed(self):
        cache = _write_cache(self.project, self.image, "DONE_VISION_ANALYZE")
        before = cache.read_text(encoding="utf-8")
        with patch.object(generator, "_run_vision_analyze") as vision:
            generator._vision_populate_bios_cache(cache, prompts_dir=self.prompts)
        vision.assert_not_called()
        self.assertEqual(cache.read_text(encoding="utf-8"), before)

    def test_analyzed_page_without_live_readings_is_not_reanalyzed(self):
        # A non-monitor BIOS page (e.g. CPU configuration) legitimately has no readings.
        cache = self.project / "P-bios-image-cache.json"
        cache.write_text(json.dumps({"items": [{
            "path": str(self.image), "filename": self.image.name,
            "analysis_status": "DONE_VISION_ANALYZE",
            "analysis_text": "No live hardware-monitor sensor rows are visible.",
            "voltage_label_hints": [], "voltage_value_hints": [],
            "temperature_value_hints": [], "fan_value_hints": [],
        }]}), encoding="utf-8")
        with patch.object(generator, "_run_vision_analyze") as vision:
            generator._vision_populate_bios_cache(cache, prompts_dir=self.prompts)
        vision.assert_not_called()

    def test_analyzed_item_needs_no_prompt_file(self):
        cache = _write_cache(self.project, self.image, "DONE_VISION_ANALYZE")
        issues: dict = {}
        with patch.object(generator, "_run_vision_analyze") as vision:
            generator._vision_populate_bios_cache(
                cache, prompt_issues=issues, prompts_dir=self.project / "no_prompts"
            )
        vision.assert_not_called()
        self.assertEqual(issues, {})

    def test_pending_item_is_analyzed_with_rules(self):
        cache = _write_cache(self.project, self.image, "PENDING_VISION_ANALYZE")
        with patch.object(generator, "_run_vision_analyze", return_value="CPU Temperature 45 C") as vision:
            generator._vision_populate_bios_cache(cache, prompts_dir=self.prompts)
        vision.assert_called_once()
        self.assertIn("bios rules", vision.call_args.args[1])
        item = json.loads(cache.read_text(encoding="utf-8"))["items"][0]
        self.assertEqual(item["analysis_status"], "DONE_VISION_ANALYZE")

    def test_pending_item_with_missing_rules_is_skipped_and_reported(self):
        cache = _write_cache(self.project, self.image, "PENDING_VISION_ANALYZE")
        issues: dict = {}
        with patch.object(generator, "_run_vision_analyze") as vision:
            generator._vision_populate_bios_cache(
                cache, prompt_issues=issues, prompts_dir=self.project / "no_prompts"
            )
        vision.assert_not_called()
        self.assertEqual(issues, {"bios_reading.md": "PROMPT_FILE_MISSING: prompts/bios_reading.md"})


class GpioTraceReuseTests(unittest.TestCase):
    def setUp(self):
        self.td = tempfile.TemporaryDirectory()
        self.project = Path(self.td.name)
        image = self.project / "circuit_gpio.png"
        image.write_bytes(b"png")
        self.trace_path = self.project / "P-gpio-trace.json"
        self.trace_path.write_text("{}", encoding="utf-8")
        later = time.time() + 60
        os.utime(self.trace_path, (later, later))

    def tearDown(self):
        self.td.cleanup()

    def test_agent_written_trace_is_used_as_is(self):
        # Shape of a trace written by the Hermes agent: no code-only meta fields.
        trace = {
            "_source_path": str(self.trace_path),
            "items": [{"signal": "SIO_GPIO0", "status": "CONFIRMED"}],
            "meta": {"source": "hermes_vision_analyze", "images_analyzed": ["circuit_gpio.png"]},
        }
        self.assertFalse(generator._should_regen_gpio_trace(self.project, "NCT6126D", trace))


if __name__ == "__main__":
    unittest.main()
