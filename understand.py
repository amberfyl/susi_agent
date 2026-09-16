# -*- coding: utf-8 -*-
"""
understand.py — Stage 1: LLM form understanding
Input : {project}.json  (from extract_pdf.py)  + optional screenshot(s)
Output: {project}-spec.json  (consumed by generate_ini.py)

LLM is configured via env vars (OpenAI-compatible):
  LLM_BASE_URL  — default: https://api.openai.com/v1
  LLM_API_KEY   — required
  LLM_MODEL     — default: gpt-5.3-codex
  LLM_API_MODE  — auto|chat|responses (default: auto)
"""

import argparse
import base64
import json
import os
import re
import sys
from pathlib import Path

# ---------------------------------------------------------------------------
# Valid ini_key sets — single source of truth for the closed key sets.
# Used in TWO places: injected into the LLM user message (build_user_message)
# AND used to validate the LLM's output afterwards (validate_spec).
# ---------------------------------------------------------------------------

VOLT_KEYS = [
    "VCORE", "VCORE2", "V25", "V33", "V50",
    "V120", "V5SB", "V3SB", "VBAT",
    "VN50", "VN120", "VTT", "V240", "DC", "DCSTBY", "VBATLI",
    "V15", "V18", "V105",
    "VOEM0", "VOEM1", "VOEM2", "VOEM3", "V5S5", "V3S5",
]
TEMP_KEYS = [
    "TCPU", "TCPU2", "TSYS", "TCHIPSET",
    "TOEM0", "TOEM1", "TOEM2", "TOEM3", "TOEM4", "TOEM5", "TOEM6",
    "GRAPHIC",
]
FAN_KEYS = [
    "FCPU", "FSYS", "FCPU2",
    "FOEM0", "FOEM1", "FOEM2", "FOEM3", "FOEM4", "FOEM5", "FOEM6",
]
_FAN_OEM_KEY_RE = re.compile(r"^FOEM(\d+)$")


def _is_valid_fan_key(key: str) -> bool:
    if not isinstance(key, str):
        return False
    return key in FAN_KEYS or bool(_FAN_OEM_KEY_RE.match(key))


CURRENT_KEYS = ["OEM0", "OEM1", "OEM2"]
CASEOPEN_KEYS = ["CO0", "CO1", "CO2"]

# Hint table shown to LLM: common signal name → ini_key
# (LLM is not limited to these — it can reason about novel names too)
VOLT_HINTS = """
Signal name patterns → ini_key:
  12V / +12V / 12VDC / VIN_12V        → V120
  5V / +5V (main)                      → V50
  5VSB / 5V_SBY / +5V Standby / 5VSBY → V5SB
  3.3V / 3V3 / +3.3V                  → V33
  3VSB / 3.3VSB / +3.3V Standby       → V3SB
  VCORE / VCore / CPU core voltage     → VCORE
  VBAT / CMOS Battery / RTC battery    → VBAT
  AVCC / Analog VCC                    → VTT
  DC / Vin / DC Input                  → DC
"""

TEMP_HINTS = """
  CPU temperature / CPU temp / CPUTIN → TCPU
  System temperature / SYS temp / SYSTIN → TSYS
  GPU / Graphics / GRAPHIC            → GRAPHIC
"""

FAN_HINTS = """
  CPU Fan / CPU_FAN                   → FCPU
  COM Module FAN                      → FCPU
  System Fan / SYS Fan / Carrier Board FAN → FSYS
  OEM Fan                             → FOEM<n> (n>=0, dynamic)
"""

CURRENT_HINTS = """
  Current / IOUT / Current sense      → OEM0
  Current1 / IOUT1                    → OEM1
  Current2 / IOUT2                    → OEM2
"""

CASEOPEN_HINTS = """
  CaseOpen / Chassis Intrusion        → CO0
  CaseOpen1                           → CO1
  CaseOpen2                           → CO2
"""

# ---------------------------------------------------------------------------
# Form JSON → readable text
# ---------------------------------------------------------------------------

def _cell(c) -> str:
    if c is None:
        return ""
    return str(c).strip()


def _table_to_text(table: list) -> str:
    lines = []
    for row in (table or []):
        cells = [_cell(c) for c in (row or [])]
        non_empty = [c for c in cells if c]
        if non_empty:
            lines.append(" | ".join(non_empty))
    return "\n".join(lines)


def form_json_to_text(form_data: dict) -> str:
    """Convert form.json into a readable text block for the LLM."""
    parts = []
    for pg in form_data.get("pages", []):
        for table in pg.get("tables", []):
            if not table or not table[0]:
                continue
            text = _table_to_text(table)
            if text.strip():
                parts.append(text)
    return "\n\n---\n\n".join(parts)


# ===========================================================================
# Prompt construction — LLM call site 2 of 2 in the pipeline
# (call site 1 is extract_pdf.py's deep-analysis pass)
#
# The message sent to the LLM is assembled from three layers:
#   1. SYSTEM_PROMPT           role + judgment rules, grouped by topic
#   2. build_analysis_hints()  grounding hints from extract_pdf.py "analysis"
#   3. build_user_message()    reference data (valid ini_key sets + hint
#                              tables above) + required output schema + form text
#
# NOTE: rule.md / spec_schema.md are human-readable docs; no code reads them.
# The operational copy of every judgment rule is HERE. When a rule changes,
# update this file first, then sync the docs.
# ===========================================================================

SYSTEM_PROMPT = """\
You are a SUSI platform configuration specialist for Advantech embedded boards.

Given a SUSI Request Form extracted from PDF (presented as table text), produce a \
spec.json that will drive automated SUSI INI file generation.

## Output contract
- Output ONLY a single valid JSON object — no markdown fences, no explanation.
- Never fabricate items not present in the form. When the form does not provide \
a value, leave the field empty rather than inventing one.

## Form template noise (never data)
- Instructional sentences addressed to the form filler — e.g. "(Only EC support)", \
"Only EC support this function and default included.", "(Please make sure HW has \
connector can verify.)", "(Driver default value only can set in OS time. Suggest set \
by BIOS to cover boot time.)" — are template text: never a chip name, label, or value. \
A "form-filler guidance" snippet list may be provided; treat everything in it the same way.
- Placeholder examples are never real values: "<Ex: ...>", "{Ex: ...}", "<Add If \
Needed>", and bare "Ex: ..." prefixes. A cell containing only such text counts as EMPTY.

## Checkbox reading
- "■" means ENABLED/CHECKED. "□" means disabled/unchecked.
- features.<key>: true if that section header has "■", false if "□" or the \
section is absent.

## HWM signal mapping (voltages / temperatures / fans / currents / caseopen)
- Map each listed signal to the correct ini_key using the hint table provided.
- If unsure, use the closest match from the valid key set.

## Chips
- chips.hwm and chips.gpio: use the chip name exactly as written in the form \
(the downstream chip DB resolves aliases). If HWM and GPIO share one chip, \
repeat the same name in both fields.

## SmartFan
- smartfan: list the ini_key of every fan that appears in the SmartFan table.

## GPIO
- gpio.count: total pin count from the GPIO section header \
(e.g. "Total:8 Pins" -> 8; "Total: 4in/4out" -> 8).
- gpio.pins: preserve GPIO rows from the form/analysis whenever available. \
One entry per known pin index. direction is one of "input" | "output" | \
"both" | "unknown". Preserve the user-entered GPIO name as `name`; if the \
form has no name or the user left it blank, use an empty string. Do not invent \
GPIO names from signal labels, chip function labels, or locations.

## Screen control
- Preserve the Screen control structure when present: record checked status \
and the table rows for LVDS Brightness / LVDS Backlight ON/OFF under \
screen_control, keeping socket/chip/remark text exactly as seen in the form.
"""


def build_analysis_hints(analysis: dict) -> str:
    """Render extract_pdf.py's deep-analysis block (if present) as prompt hints.

    This is grounding, not a shortcut: the LLM still derives spec.json from the raw
    form text below. Hints just save it from re-guessing chip roles/GPIO facts that
    extract_pdf.py already worked out from text + embedded images.
    """
    if not analysis:
        return ""

    chips = analysis.get("chips", {}) or {}
    features = analysis.get("susi_features", {}) or {}
    gpio = analysis.get("gpio", {}) or {}

    lines = ["## Hints from prior deep analysis (extract_pdf.py) — verify against the form text below, do not blindly copy"]
    lines.append(f"chips: superio={chips.get('superio')!r} ec={chips.get('ec')!r} pch={chips.get('pch')!r} "
                 f"other={chips.get('other')!r}")
    if gpio.get("total_pins") is not None:
        lines.append(f"gpio.total_pins={gpio.get('total_pins')!r}, pins={gpio.get('pins')!r}")
    if features:
        status_line = ", ".join(f"{k}={v.get('status')!r}/{v.get('chip')!r}" for k, v in features.items())
        lines.append(f"susi_features (status/chip): {status_line}")
    if analysis.get("unresolved"):
        lines.append(f"Known unresolved items: {analysis['unresolved']!r}")
    if analysis.get("conflicts"):
        lines.append(f"Known conflicts: {analysis['conflicts']!r}")

    return "\n".join(lines) + "\n"


def build_guidance_hints(guidance: list | None) -> str:
    """Render extract_pdf.py's red-print snippet list as an ignore-list hint."""
    if not guidance:
        return ""
    lines = ["## Form-filler guidance snippets (red print in the PDF) — template instructions, never data values"]
    lines += [f"- {g}" for g in guidance[:40]]
    return "\n".join(lines) + "\n"


def build_user_message(form_text: str, analysis_hints: str = "") -> str:
    hints_block = f"\n{analysis_hints}\n" if analysis_hints else ""
    return f"""\
{hints_block}## Valid ini_key sets

Voltages : {VOLT_KEYS}
{VOLT_HINTS}
Temperatures: {TEMP_KEYS}
{TEMP_HINTS}
Fans: {FAN_KEYS}
{FAN_HINTS}
Currents: {CURRENT_KEYS}
{CURRENT_HINTS}
CaseOpen: {CASEOPEN_KEYS}
{CASEOPEN_HINTS}

## Required output schema

```json
{{
  "version": "1.0",
  "project": "<project name>",
  "platform": "<platform string>",
  "information": {{
    "PlatformVersion": "<string, can be empty>",
    "BIOSVersion": "<string, can be empty>",
    "ECVersion": "<string, can be empty>"
  }},
  "chips": {{
    "hwm": "<HWM chip name from form>",
    "gpio": "<GPIO chip name from form>"
  }},
  "voltages":     [{{"ini_key": "V120", "label": "<form label>"}}],
  "temperatures": [{{"ini_key": "TCPU", "label": "<form label>"}}],
  "fans":         [{{"ini_key": "FCPU", "label": "<form label>"}}],
  "currents":     [{{"ini_key": "OEM0", "label": "<form label>"}}],
  "caseopen":     [{{"ini_key": "CO0", "label": "<form label>"}}],
  "smartfan":     ["<ini_key>", ...],
  "gpio": {{
    "count": 8,
        "pins": [{{"index": 0, "direction": "input|output|both|unknown", "chip": "ITE8528", "location": "0", "name": "<user-entered name or empty>"}}]
  }},
  "features": {{
    "smbus": true,
    "i2c": true,
    "backlight": true,
    "brightness": true,
    "wdt": true,
    "wdt_pinevent": true,
    "storage": true,
    "thermalprotect": true
  }},
  "feature_details": {{
    "smbus": {{"status": true, "chip": "<EC/model/description text if present>"}}
  }},
  "screen_control": {{
    "brightness": {{
      "enabled": true,
      "items": [{{"socket": "LVDS1", "chip": "PTN3460", "remark": ""}}]
    }},
    "backlight": {{
      "enabled": false,
      "items": [{{"socket": "<Ex: CN01>", "chip": "<Ex: EC >", "remark": ""}}]
    }}
  }}
}}
```

## SUSI Request Form content

{form_text}

Now output the spec.json:"""


# ---------------------------------------------------------------------------
# LLM call
# ---------------------------------------------------------------------------

def _response_output_text(response) -> str:
    txt = getattr(response, "output_text", None)
    if txt:
        return txt

    chunks = []
    for item in (getattr(response, "output", None) or []):
        for c in (getattr(item, "content", None) or []):
            t = getattr(c, "text", None)
            if t:
                chunks.append(t)

    if chunks:
        return "\n".join(chunks)
    return str(response)


def call_llm(user_message: str, screenshots: list[Path]) -> str:
    """Call the configured LLM and return the raw text response."""
    try:
        from openai import OpenAI
    except ImportError:
        sys.exit("openai package not installed — run: pip install openai")

    api_key  = os.environ.get("LLM_API_KEY", "")
    base_url = os.environ.get("LLM_BASE_URL", "https://api.openai.com/v1")
    model    = os.environ.get("LLM_MODEL", "gpt-5.3-codex")

    api_mode = os.environ.get("LLM_API_MODE", "auto").strip().lower()

    if not api_key:
        sys.exit("LLM_API_KEY not set")

    client = OpenAI(api_key=api_key, base_url=base_url)

    # Build content blocks
    content: list = [{"type": "text", "text": user_message}]

    for img_path in screenshots:
        if not img_path.exists():
            print(f"Warning: screenshot not found: {img_path}", file=sys.stderr)
            continue
        suffix = img_path.suffix.lower()
        mime = {"jpg": "image/jpeg", ".jpeg": "image/jpeg",
                ".png": "image/png", ".webp": "image/webp"}.get(suffix, "image/png")
        b64 = base64.b64encode(img_path.read_bytes()).decode()
        content.append({
            "type": "image_url",
            "image_url": {"url": f"data:{mime};base64,{b64}"},
        })

    # Keep legacy Chat Completions path unchanged unless model/mode requires Responses.
    # gpt-5.x (e.g., gpt-5.3-codex) is unsupported on chat.completions in this endpoint.
    use_responses = (
        api_mode == "responses"
        or (api_mode == "auto" and model.startswith("gpt-5"))
    )

    if use_responses:
        resp_input = [{"type": "input_text", "text": user_message}]
        for img_path in screenshots:
            if not img_path.exists():
                continue
            suffix = img_path.suffix.lower().lstrip(".")
            mime = {"jpg": "image/jpeg", "jpeg": "image/jpeg",
                    "png": "image/png", "webp": "image/webp"}.get(suffix, "image/png")
            b64 = base64.b64encode(img_path.read_bytes()).decode()
            resp_input.append({
                "type": "input_image",
                "image_url": f"data:{mime};base64,{b64}",
            })

        response = client.responses.create(
            model=model,
            instructions=SYSTEM_PROMPT,
            input=[{"role": "user", "content": resp_input}],
            text={"format": {"type": "json_object"}},
            temperature=0.1,
            max_output_tokens=4096,
        )
        return _response_output_text(response)

    response = client.chat.completions.create(
        model=model,
        messages=[
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user",   "content": content},
        ],
        response_format={"type": "json_object"},
        temperature=0.1,
    )
    return response.choices[0].message.content


# ---------------------------------------------------------------------------
# Output validation (lightweight)
# ---------------------------------------------------------------------------

def validate_spec(spec: dict) -> list[str]:
    warnings = []
    valid_volt  = set(VOLT_KEYS)

    info = spec.get("information")
    if info is not None and not isinstance(info, dict):
        warnings.append("information should be an object")
    elif isinstance(info, dict):
        for k in ("PlatformVersion", "BIOSVersion", "ECVersion"):
            if k in info and not isinstance(info.get(k), str):
                warnings.append(f"information.{k} should be string")
    gpio = spec.get("gpio")
    if gpio is not None and not isinstance(gpio, dict):
        warnings.append("gpio should be an object")
    elif isinstance(gpio, dict):
        pins = gpio.get("pins", [])
        if pins is not None and not isinstance(pins, list):
            warnings.append("gpio.pins should be an array")
        elif isinstance(pins, list):
            for pin in pins:
                if isinstance(pin, dict) and "name" in pin and not isinstance(pin.get("name"), str):
                    warnings.append("gpio pin name should be string")

    valid_temp  = set(TEMP_KEYS)
    valid_fan   = set(FAN_KEYS)
    valid_curr  = set(CURRENT_KEYS)
    valid_case  = set(CASEOPEN_KEYS)

    for item in spec.get("voltages", []):
        if item.get("ini_key") not in valid_volt:
            warnings.append(f"Unknown voltage ini_key: {item.get('ini_key')!r}")
    for item in spec.get("temperatures", []):
        if item.get("ini_key") not in valid_temp:
            warnings.append(f"Unknown temperature ini_key: {item.get('ini_key')!r}")
    for item in spec.get("fans", []):
        key = item.get("ini_key")
        if not _is_valid_fan_key(key):
            warnings.append(f"Unknown fan ini_key: {key!r}")
    for item in spec.get("currents", []):
        if item.get("ini_key") not in valid_curr:
            warnings.append(f"Unknown current ini_key: {item.get('ini_key')!r}")
    for item in spec.get("caseopen", []):
        if item.get("ini_key") not in valid_case:
            warnings.append(f"Unknown caseopen ini_key: {item.get('ini_key')!r}")
    for key in spec.get("smartfan", []):
        if not _is_valid_fan_key(key):
            warnings.append(f"Unknown smartfan ini_key: {key!r}")

    if not spec.get("chips", {}).get("hwm"):
        warnings.append("chips.hwm is empty")

    # Placeholder/example text must never survive into spec values.
    placeholder = re.compile(r"^\s*[<{(\[]?\s*Ex\s*[:：]", re.IGNORECASE)
    for chip_field, chip_val in (spec.get("chips") or {}).items():
        if isinstance(chip_val, str) and placeholder.match(chip_val):
            warnings.append(f"chips.{chip_field} looks like a placeholder example: {chip_val!r}")
    for list_name in ("voltages", "temperatures", "fans", "currents", "caseopen"):
        for item in spec.get(list_name, []):
            label = item.get("label", "")
            if isinstance(label, str) and placeholder.match(label):
                warnings.append(f"{list_name} label looks like a placeholder example: {label!r}")

    # Optional: screen_control structure (forward-compatible)
    sc = spec.get("screen_control")
    if sc is not None and not isinstance(sc, dict):
        warnings.append("screen_control should be an object")
    elif isinstance(sc, dict):
        for sec in ("brightness", "backlight"):
            node = sc.get(sec)
            if node is None:
                continue
            if not isinstance(node, dict):
                warnings.append(f"screen_control.{sec} should be an object")
                continue
            if "enabled" in node and not isinstance(node.get("enabled"), bool):
                warnings.append(f"screen_control.{sec}.enabled should be boolean")
            items = node.get("items", [])
            if items is not None and not isinstance(items, list):
                warnings.append(f"screen_control.{sec}.items should be array")

    return warnings


# ---------------------------------------------------------------------------
# Path resolution (same convention as generate_ini.py)
# ---------------------------------------------------------------------------

def _extract_gpio_pins_from_pages(form_data: dict) -> list[dict]:
    """Extract GPIO pin rows directly from page tables in form.json.

    This is a deterministic fallback for cases where analysis.gpio only keeps a
    subset (e.g., 0..7) but page tables still contain additional rows (e.g., 8..15).
    """
    pages = form_data.get("pages") or []
    out: dict[int, dict] = {}

    def _gpio_index(value) -> int | None:
        text = str(value or "").strip()
        if re.fullmatch(r"\d+", text):
            return int(text)
        if not re.match(r"^\s*(?:GPI|GPO|GPIO|GP)", text, re.IGNORECASE):
            return None
        numbers = re.findall(r"\d+", text)
        return int(numbers[-1]) if numbers else None

    for page in pages:
        tables = page.get("tables") or []
        for table in tables:
            if not isinstance(table, list):
                continue

            # Try to discover header column indexes dynamically.
            pin_col = chip_col = loc_col = sup_col = name_col = None
            for row in table:
                if not isinstance(row, list):
                    continue
                cells = [(c or "").strip() if isinstance(c, str) else str(c) for c in row]
                lowered = [c.lower() for c in cells]

                if pin_col is None and any("pin" == c for c in lowered):
                    pin_col = lowered.index("pin")
                if chip_col is None and any("chip" == c for c in lowered):
                    chip_col = lowered.index("chip")
                if loc_col is None and any("location" == c for c in lowered):
                    loc_col = lowered.index("location")
                if sup_col is None and any("support" == c for c in lowered):
                    sup_col = lowered.index("support")
                if name_col is None:
                    for candidate in ("name", "gpio name", "signal name"):
                        if candidate in lowered:
                            name_col = lowered.index(candidate)
                            break

                if pin_col is None:
                    # Heuristic for continuation tables where header is not repeated:
                    # common GPIO row layout is ['', GPI0/GPO0, default, support, chip, location, ...]
                    if len(cells) > 1 and _gpio_index(cells[1]) is not None:
                        pin_col = 1
                        if chip_col is None and len(cells) > 4:
                            chip_col = 4
                        if loc_col is None and len(cells) > 5:
                            loc_col = 5
                        if sup_col is None and len(cells) > 3:
                            sup_col = 3
                        if name_col is None and len(cells) > 6:
                            name_col = 6
                    else:
                        continue
                if pin_col >= len(cells):
                    continue

                idx = _gpio_index(cells[pin_col])
                if idx is None:
                    continue

                direction = "unknown"
                if sup_col is not None and sup_col < len(cells):
                    s = lowered[sup_col]
                    has_in = "input" in s
                    has_out = "output" in s
                    has_both = "both" in s
                    if has_both or (has_in and has_out):
                        direction = "both"
                    elif has_in:
                        direction = "input"
                    elif has_out:
                        direction = "output"

                row = {
                    "index": idx,
                    "direction": direction,
                    "name": "",
                }
                if chip_col is not None and chip_col < len(cells) and cells[chip_col]:
                    row["chip"] = cells[chip_col]
                if loc_col is not None and loc_col < len(cells) and cells[loc_col]:
                    row["location"] = cells[loc_col]
                if name_col is not None and name_col < len(cells) and cells[name_col]:
                    row["name"] = cells[name_col]

                prev = out.get(idx)
                if prev is None:
                    out[idx] = row
                else:
                    # Prefer concrete direction/chip/location over unknown/empty.
                    if prev.get("direction") == "unknown" and row.get("direction") != "unknown":
                        prev["direction"] = row["direction"]
                    if not prev.get("chip") and row.get("chip"):
                        prev["chip"] = row["chip"]
                    if not prev.get("location") and row.get("location"):
                        prev["location"] = row["location"]
                    if not prev.get("name") and row.get("name"):
                        prev["name"] = row["name"]

    return [out[i] for i in sorted(out.keys())]


def enrich_gpio_from_analysis(spec: dict, form_data: dict) -> None:
    """Backfill gpio details from extract output when LLM output is sparse.

    Sources are merged conservatively:
    1) analysis.gpio.pins
    2) page table parsing (can recover extra rows like GPIO8..15)
    """
    analysis_gpio = ((form_data.get("analysis") or {}).get("gpio") or {})

    spec_gpio = spec.get("gpio")
    if not isinstance(spec_gpio, dict):
        spec_gpio = {}
        spec["gpio"] = spec_gpio

    merged: dict[int, dict] = {}

    def _merge_pin(row: dict):
        if not isinstance(row, dict):
            return
        idx = row.get("index")
        try:
            idx = int(idx)
        except (TypeError, ValueError):
            return
        direction = (row.get("direction") or "unknown").lower()
        if direction not in {"input", "output", "both", "unknown"}:
            direction = "unknown"

        cur = merged.get(idx, {"index": idx, "direction": "unknown"})
        if cur.get("direction") == "unknown" and direction != "unknown":
            cur["direction"] = direction
        elif "direction" not in cur:
            cur["direction"] = direction
        if not cur.get("chip") and row.get("chip"):
            cur["chip"] = row.get("chip")
        if not cur.get("location") and row.get("location") is not None:
            cur["location"] = str(row.get("location"))
        if not cur.get("name") and row.get("name") is not None:
            cur["name"] = str(row.get("name"))
        merged[idx] = cur

    existing_pins = spec_gpio.get("pins")
    if isinstance(existing_pins, list):
        for p in existing_pins:
            if not isinstance(p, dict):
                continue
            _merge_pin({
                "index": p.get("index"),
                "direction": p.get("direction"),
                "chip": p.get("chip"),
                "location": p.get("location"),
                "name": p.get("name", ""),
            })

    # Source 1: analysis.gpio.pins
    src_pins = analysis_gpio.get("pins") if isinstance(analysis_gpio, dict) else []
    if isinstance(src_pins, list):
        for p in src_pins:
            if not isinstance(p, dict):
                continue
            _merge_pin({
                "index": p.get("pin"),
                "direction": p.get("direction"),
                "chip": p.get("chip"),
                "location": p.get("location"),
                "name": p.get("name", ""),
            })

    # Source 2: parse page tables directly
    for p in _extract_gpio_pins_from_pages(form_data):
        _merge_pin(p)

    if merged:
        out = [merged[i] for i in sorted(merged.keys())]
        spec_gpio["pins"] = out

        max_pin = max(merged.keys()) + 1
        count_candidates = [max_pin]
        if isinstance(analysis_gpio, dict) and isinstance(analysis_gpio.get("total_pins"), int):
            count_candidates.append(analysis_gpio.get("total_pins"))
        if isinstance(spec_gpio.get("count"), int):
            count_candidates.append(spec_gpio.get("count"))
        spec_gpio["count"] = max(count_candidates)


def ensure_information_block(spec: dict, form_data: dict) -> None:
    """Ensure spec has a stable top-level information object.

    This stage only guarantees schema presence and low-confidence placeholders.
    Priority merge (probe > bios > pdf) is handled by orchestrator later.
    """
    info = spec.get("information")
    if not isinstance(info, dict):
        info = {}

    analysis = (form_data.get("analysis") or {}) if isinstance(form_data, dict) else {}

    platform = (
        info.get("PlatformVersion")
        or spec.get("platform")
        or spec.get("project")
        or ""
    )
    bios = (
        info.get("BIOSVersion")
        or analysis.get("bios_version")
        or ""
    )
    ec = (
        info.get("ECVersion")
        or analysis.get("ec_version")
        or ""
    )

    spec["information"] = {
        "PlatformVersion": str(platform).strip(),
        "BIOSVersion": str(bios).strip(),
        "ECVersion": str(ec).strip(),
    }


def ensure_feature_details(spec: dict, form_data: dict) -> None:
    """Preserve non-boolean feature details from extracted analysis.

    Keep backward compatibility by leaving `features.<key>` as booleans, and
    storing richer source metadata under `feature_details`.
    """
    analysis = (form_data.get("analysis") or {}) if isinstance(form_data, dict) else {}
    susi_features = analysis.get("susi_features") if isinstance(analysis, dict) else None
    if not isinstance(susi_features, dict):
        return

    details = spec.get("feature_details")
    if not isinstance(details, dict):
        details = {}

    for key, node in susi_features.items():
        if not isinstance(node, dict):
            continue

        status = node.get("status")
        # Normalize status for JSON stability.
        if isinstance(status, bool):
            norm_status = status
        elif isinstance(status, str):
            s = status.strip().lower()
            if s in {"true", "yes", "1", "enabled", "checked"}:
                norm_status = True
            elif s in {"false", "no", "0", "disabled", "unchecked"}:
                norm_status = False
            else:
                norm_status = "unknown"
        else:
            norm_status = "unknown"

        chip = node.get("chip")
        chip_text = "" if chip is None else str(chip).strip()

        out_node = {
            "status": norm_status,
            "chip": chip_text,
        }

        # Preserve any additional extractor-provided context if present.
        for extra_key in ("remark", "desc", "description", "note"):
            if extra_key in node and node.get(extra_key) is not None:
                out_node[extra_key] = str(node.get(extra_key)).strip()

        details[key] = out_node

    spec["feature_details"] = details


def resolve_paths(project: str | None, in_json: str | None, spec_out: str | None,
                  root: Path) -> tuple[Path, Path]:
    if in_json:
        json_path = Path(in_json) if Path(in_json).is_absolute() else root / in_json
        name = json_path.stem.replace("-spec", "").replace("-pre", "")
        proj_dir = json_path.parent
    elif project:
        p = Path(project)
        if p.is_absolute() or (root / p).is_dir():
            proj_dir = p if p.is_absolute() else root / p
            name = proj_dir.name
            json_path = proj_dir / f"{name}.json"
        else:
            name = project
            subdir = root / name / f"{name}.json"
            flat   = root / f"{name}.json"
            json_path = subdir if subdir.exists() else flat
            proj_dir = json_path.parent
    else:
        sys.exit("Provide --project or --in-json")

    if spec_out:
        out_path = Path(spec_out) if Path(spec_out).is_absolute() else root / spec_out
    else:
        out_path = proj_dir / f"{name}-spec.json"

    return json_path, out_path


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def understand(project: str | None, in_json: str | None, spec_out: str | None,
               screenshots: list[Path], root: Path) -> Path:
    json_path, out_path = resolve_paths(project, in_json, spec_out, root)

    if not json_path.exists():
        sys.exit(f"form.json not found: {json_path}")

    with open(json_path, encoding="utf-8") as f:
        form_data = json.load(f)

    # Derive project name from path if not given
    proj_name = project or json_path.stem

    form_text   = form_json_to_text(form_data)
    analysis_hints = build_analysis_hints(form_data.get("analysis"))
    analysis_hints += build_guidance_hints(form_data.get("form_guidance"))
    user_msg    = build_user_message(form_text, analysis_hints)
    # Inject project name hint into the prompt
    user_msg    = f"Project name: {proj_name}\n\n" + user_msg

    print(f"Calling LLM ({os.environ.get('LLM_MODEL', 'gpt-5.3-codex')}) …", file=sys.stderr)
    raw = call_llm(user_msg, screenshots)

    try:
        spec = json.loads(raw)
    except json.JSONDecodeError as e:
        sys.exit(f"LLM returned invalid JSON: {e}\n\nRaw output:\n{raw[:500]}")

    # Ensure project field is set
    if not spec.get("project"):
        spec["project"] = proj_name

    # Preserve detailed GPIO rows extracted from PDF analysis when LLM output is sparse.
    enrich_gpio_from_analysis(spec, form_data)

    # Ensure spec.json always carries a stable [Information] JSON block.
    ensure_information_block(spec, form_data)

    # Preserve richer non-boolean feature metadata (e.g., smbus chip/description)
    # from extract analysis while keeping features booleans backward-compatible.
    ensure_feature_details(spec, form_data)

    warnings = validate_spec(spec)
    for w in warnings:
        print(f"Warning: {w}", file=sys.stderr)

    out_path.parent.mkdir(parents=True, exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(spec, f, ensure_ascii=False, indent=2)

    print(f"Wrote: {out_path}", file=sys.stderr)
    return out_path


def main():
    root = Path(__file__).parent

    parser = argparse.ArgumentParser(
        description="Stage 1: LLM form understanding → spec.json"
    )
    parser.add_argument("--project",  help="Project name or directory path")
    parser.add_argument("--in-json",  dest="in_json", help="Override input form.json path")
    parser.add_argument("--spec-out", dest="spec_out", help="Override output spec.json path")
    parser.add_argument("--root",     default=str(root), help="Base directory")
    parser.add_argument("--screenshot", dest="screenshots", action="append",
                        metavar="PATH", default=[],
                        help="Optional screenshot(s) to include (repeatable)")
    args = parser.parse_args()

    screenshots = [Path(s) for s in args.screenshots]
    understand(
        project=args.project,
        in_json=args.in_json,
        spec_out=args.spec_out,
        screenshots=screenshots,
        root=Path(args.root),
    )


if __name__ == "__main__":
    main()
