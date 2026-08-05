"""
extract_pdf.py — Stage 0: PDF -> project JSON

Output shape:
{
  "pages": [ {page, text, tables}, ... ],   # raw pdfplumber extraction (unchanged format —
                                             # generate_ini.py's form.json fallback and
                                             # understand.py's form_json_to_text() both read
                                             # this key directly, so its shape must not change)
  "analysis": { ... }                       # NEW: deep chip/GPIO/SUSI-feature analysis (LLM,
                                             # vision-capable — reads raw text/tables + any
                                             # embedded images extracted from the PDF)
}

LLM is configured via env vars (OpenAI-compatible), same convention as understand.py:
  LLM_BASE_URL  — default: https://api.openai.com/v1
  LLM_API_KEY   — required
  LLM_MODEL     — default: gpt-5.3-codex
  LLM_API_MODE  — auto|chat|responses (default: auto)
"""

import argparse
import base64
import json
import os
import sys
from pathlib import Path

TEMPLATE_PLACEHOLDER_HINT = (
    'Text like "<Ex: ...>", "{Ex: ...}", "<Add If Needed>", or a bare "Ex: ..." prefix '
    "is unfilled template boilerplate, never a real value."
)

# Known form-guidance boilerplate: instructional sentences addressed to the
# person FILLING the form (typically printed in red in the PDF). Never board
# data. Two detection layers: this phrase list (prompt rule) and red-print
# style detection (collect_form_guidance). NOTE: do NOT strip these from the
# raw "pages" content — generate_ini.py's conservative feature fallback
# matches on "Only EC support this function and default included.".
FORM_GUIDANCE_PHRASES = [
    "(Only EC support)",
    "Only EC support this function and default included.",
    "(Please make sure HW has connector can verify.)",
    "(Driver default value only can set in OS time. Suggest set by BIOS to cover boot time.)",
]

SUSI_FEATURE_KEYS = [
    "hardware_monitor", "gpio", "i2c", "smbus", "lvds_backlight",
    "watchdog", "storage_eeprom", "pinevent", "thermal_protect",
    "smartfan", "canbus",
]

GPIO_DIRECTIONS = ["input", "output", "follow_bios", "both", "unknown"]


# ---------------------------------------------------------------------------
# Path resolution (unchanged from prior version)
# ---------------------------------------------------------------------------

def resolve_paths(project: str | None, input_pdf: str | None, out_json: str | None, root: Path):
    """
    Flexible path resolution. --project accepts:
      - plain name       "SOM-9590"           → tries root/SOM-9590/, then root/
      - absolute dir      "/abs/path/SOM-9590" → looks for SOM-9590.pdf inside
      - absolute .pdf     "/abs/path/foo.pdf"  → used directly
    """
    if input_pdf:
        pdf_path = Path(input_pdf)
        if not pdf_path.is_absolute():
            pdf_path = root / input_pdf
        name = pdf_path.stem
        proj_dir = pdf_path.parent
    elif project:
        p = Path(project)
        if p.suffix == ".pdf":
            pdf_path = p if p.is_absolute() else root / p
            name = pdf_path.stem
            proj_dir = pdf_path.parent
        elif p.is_absolute() or (root / p).is_dir():
            proj_dir = p if p.is_absolute() else root / p
            name = proj_dir.name
            pdf_path = proj_dir / f"{name}.pdf"
        else:
            name = project
            subdir_pdf = root / name / f"{name}.pdf"
            flat_pdf   = root / f"{name}.pdf"
            pdf_path = subdir_pdf if subdir_pdf.exists() else flat_pdf
            proj_dir = pdf_path.parent
    else:
        name = "form"
        pdf_path = root / "form.pdf"
        proj_dir = root

    if out_json:
        json_path = Path(out_json)
        if not json_path.is_absolute():
            json_path = root / out_json
    else:
        json_path = proj_dir / f"{name}.json"

    return pdf_path, json_path


# ---------------------------------------------------------------------------
# Raw extraction (pdfplumber) — same shape as the old extract_pdf.py
# ---------------------------------------------------------------------------

def extract_raw_pages(pdf_path: Path) -> dict:
    import pdfplumber

    if not pdf_path.exists():
        raise FileNotFoundError(f"Input PDF not found: {pdf_path}")

    result = {"pages": []}
    with pdfplumber.open(str(pdf_path)) as pdf:
        for i, page in enumerate(pdf.pages):
            text = page.extract_text() or ""
            page_data = {"page": i + 1, "text": text, "tables": []}
            for table in page.extract_tables():
                cleaned = [[cell if cell else "" for cell in row] for row in table]
                page_data["tables"].append(cleaned)
            result["pages"].append(page_data)
    return result


# ---------------------------------------------------------------------------
# Red-print form-guidance detection (pdfplumber char level)
# ---------------------------------------------------------------------------

def _is_reddish(color) -> bool:
    """True for red-dominant fill colors (RGB or CMYK). Gray/None -> False."""
    if not isinstance(color, (list, tuple)):
        return False
    if len(color) == 3:
        r, g, b = color
        return r >= 0.5 and g <= 0.4 and b <= 0.4
    if len(color) == 4:
        c, m, y, k = color
        return c <= 0.3 and m >= 0.5 and y >= 0.3 and k <= 0.5
    return False


def collect_form_guidance(pdf_path: Path) -> list[str]:
    """Collect text runs printed in red — form-filler guidance, not board data.

    Character style survives only at pdfplumber's char level (extract_text /
    extract_tables drop it), so this is a separate pass. Only red counts as a
    style signal: bold alone is NOT used, because section headers are bold.
    The raw "pages" content is deliberately left untouched (see the note on
    FORM_GUIDANCE_PHRASES); the snippets are fed to the LLM as an ignore-list.
    """
    import pdfplumber

    snippets: list[str] = []
    with pdfplumber.open(str(pdf_path)) as pdf:
        for page in pdf.pages:
            lines: dict[int, list] = {}
            for ch in page.chars:
                if not _is_reddish(ch.get("non_stroking_color")):
                    continue
                lines.setdefault(round(ch.get("top", 0)), []).append(ch)
            for _, chs in sorted(lines.items()):
                text = "".join(c.get("text", "")
                               for c in sorted(chs, key=lambda c: c.get("x0", 0))).strip()
                if len(text) >= 6 and text not in snippets:
                    snippets.append(text)
    return snippets


# ---------------------------------------------------------------------------
# Embedded image extraction (PyMuPDF) — only real embedded image objects,
# not full-page renders.
# ---------------------------------------------------------------------------

def extract_embedded_images(pdf_path: Path, out_dir: Path, name: str) -> list[Path]:
    import fitz  # PyMuPDF

    out_dir.mkdir(parents=True, exist_ok=True)
    saved: list[Path] = []

    doc = fitz.open(str(pdf_path))
    try:
        for page_index in range(len(doc)):
            page = doc[page_index]
            for img_index, img in enumerate(page.get_images(full=True), start=1):
                xref = img[0]
                base_image = doc.extract_image(xref)
                ext = base_image["ext"]
                img_path = out_dir / f"{name}_p{page_index + 1}_img{img_index}.{ext}"
                img_path.write_bytes(base_image["image"])
                saved.append(img_path)
    finally:
        doc.close()

    return saved


# ---------------------------------------------------------------------------
# Raw pages -> readable text digest (fed to the LLM alongside the images)
# ---------------------------------------------------------------------------

def _cell(c) -> str:
    return "" if c is None else str(c).strip()


def _table_to_text(table: list) -> str:
    lines = []
    for row in (table or []):
        cells = [_cell(c) for c in (row or [])]
        non_empty = [c for c in cells if c]
        if non_empty:
            lines.append(" | ".join(non_empty))
    return "\n".join(lines)


def raw_pages_to_text(raw: dict) -> str:
    parts = []
    for pg in raw.get("pages", []):
        page_no = pg.get("page")
        text = (pg.get("text") or "").strip()
        table_texts = [_table_to_text(t) for t in pg.get("tables", [])]
        table_texts = [t for t in table_texts if t.strip()]
        block = f"--- Page {page_no} plain text ---\n{text}"
        if table_texts:
            block += "\n\n--- Page {} tables ---\n{}".format(
                page_no, "\n\n".join(table_texts))
        parts.append(block)
    return "\n\n".join(parts)


# ===========================================================================
# Prompt construction — LLM call site 1 of 2 in the pipeline
# (call site 2 is understand.py's spec.json pass)
#
# These are document-READING rules: how to interpret a messy, hand-filled
# SUSI Request Form (checkbox noise, copy-paste errors, template leftovers).
# Every rule below was derived from validating 6 real PDFs — see
# rule.md for the human-readable version. No code reads the .md docs;
# the operational copy of every rule is HERE. When a rule changes,
# update this file first, then sync the docs.
# ===========================================================================

SYSTEM_PROMPT = f"""\
You are a hardware document parsing assistant for Advantech SUSI embedded boards. \
You are given the extracted text/tables of a SUSI Request Specification PDF, plus any \
images embedded in that PDF (block diagrams, schematics, photos). Produce ONE JSON object \
analyzing the document.

## Output contract
- Output ONLY the JSON object — no markdown fences, no explanation.
- Only use what is visible in the given text/tables/images. Never guess or fabricate a \
value that is not derivable from the document. If unknown, use "unknown" (or null / \
empty list, matching the field's type).
- {TEMPLATE_PLACEHOLDER_HINT} Never copy placeholder text into an output field.

## Form-filler guidance (template text, not data)
- These forms embed instructional sentences addressed to the person filling the form, \
typically printed in red, e.g.: {FORM_GUIDANCE_PHRASES}. Never treat such a sentence \
as a chip name, label, or field value.
- The user message may include a "Red-print snippets" list detected from the PDF's \
character styles — treat every snippet in it the same way: guidance, not data.
- Guidance text may still carry a semantic hint about its section (e.g. "Only EC \
support this function and default included." implies the feature is EC-backed); \
use it only as context, never as a value.

## Checkbox reading
- These forms use "■" (filled) = selected and "□" (hollow) = not selected.
- Some documents leave a leftover "□" glyph next to a filled "■" because the requestor's \
deletion did not survive PDF text extraction — this is normal noise, NOT a conflict.
- The rule is simply: if "■" appears anywhere for that item -> true. If only "□" appears \
(no "■" anywhere for that item) -> false. If neither appears (topic not mentioned at \
all) -> "unknown". Do not report "■□ both present" as a conflict.

## When to use "conflicts" vs "unresolved"
- "conflicts": ONLY when two DIFFERENT, genuinely independent signals contradict each \
other in a way the checkbox rule cannot resolve — e.g. a section's own header checkbox \
reads as disabled (only "□", no "■") but a sub-item checkbox or filled-in real data \
under that same section clearly indicates it IS in use. Never use "conflicts" for \
ordinary "■□" glyph noise (see "Checkbox reading").
- "unresolved": data-quality problems — e.g. a "Chip" column value that is obviously \
not a plausible chip for that feature (a SPI NOR flash part number like "W25Q16JVSSIQ" \
or "Macronix MX25L25673GM2I-08G" showing up as the chip for SmartFan/I2C/Watchdog is \
almost certainly a copy-paste or PDF layout artifact, not the real chip — do not use it \
as the feature's chip; explain why in "unresolved" instead), or any field you could not \
confidently determine and why.

## Chip classification
- superio: SuperIO monitoring chip (e.g. Nuvoton/Winbond/ITE parts used for HWM).
- ec: Embedded Controller.
- pch: Platform Controller Hub — ONLY when the document explicitly says "PCH" \
(e.g. "INTEL_TIGERLAKE PCH H"). Do not guess a chip is the PCH just because it sounds \
like an Intel platform name — e.g. "Intel BoardWell" with no "PCH" wording goes into \
"other", not "pch".
- other: any other real chip mentioned (I2C/GPIO expanders like TCA9554, LVDS/eDP \
bridge chips like PTN3460, or generic mentions like "CPU"/"SOC" used in place of a \
specific part number) — give a short role_hint for each.

## GPIO
- GPIO has no "group" concept in these forms — emit a flat list of pins.
- Each pin carries its own chip (a single GPIO table can span two different chips, \
e.g. EC pins then PCH/SoC pins), its location string exactly as written (bank name, \
pin index, or "CNxx PinY" — whatever granularity the form gives), and its direction.
- direction is one of {GPIO_DIRECTIONS} — "follow_bios" and "both" are literal states \
the form offers (Default value: Follow BIOS; Support: Both). Use "unknown" only when \
no direction checkbox for that pin has "■" at all.

## SUSI features
- susi_features status is a tri-state per "Checkbox reading": true / false / "unknown".
- "chip" is the responsible chip name if determinable, else null.
- Do not assume "has an EC" implies Thermal Protect / Storage(EEPROM) are enabled — \
some documents explicitly disable them even with an EC present; read that section's \
own checkbox/text.

## Images
- images_summary: one short sentence per embedded image — what type it is (block \
diagram / schematic / photo / other) and its engineering meaning. No page numbers, \
no per-image confidence, no raw OCR dump — just the takeaway.

Required JSON schema:
{{
  "file_name": "<pdf file name>",
  "platform": "<platform name>",
  "summary": "<one sentence: what this document covers>",
  "bios_version": "<string or 'unknown'>",
  "chips": {{
    "superio": "<name or 'unknown'>",
    "ec": "<name or 'unknown'>",
    "pch": "<name or 'unknown'>",
    "other": [{{"name": "<chip name>", "role_hint": "<short role>"}}]
  }},
  "power_state": {{"s3": true, "s4": "unknown"}},
  "susi_features": {{
    "hardware_monitor": {{"status": true, "chip": "<name or null>"}},
    "gpio":             {{"status": true, "chip": "<name or null>"}},
    "i2c":              {{"status": true, "chip": "<name or null>"}},
    "smbus":            {{"status": true, "chip": "<name or null>"}},
    "lvds_backlight":   {{"status": true, "chip": "<name or null>"}},
    "watchdog":         {{"status": true, "chip": "<name or null>"}},
    "storage_eeprom":   {{"status": true, "chip": "<name or null>"}},
    "pinevent":         {{"status": true, "chip": "<name or null>"}},
    "thermal_protect":  {{"status": true, "chip": "<name or null>"}},
    "smartfan":         {{"status": true, "chip": "<name or null>"}},
    "canbus":           {{"status": true, "chip": "<name or null>"}}
  }},
  "gpio": {{
    "total_pins": <integer>,
    "pins": [{{"pin": "0", "chip": "<name>", "location": "<as written>", "direction": "input|output|both|unknown"}}]
  }},
  "images_summary": ["<one sentence per embedded image>"],
  "unresolved": ["<field/reason>"],
  "conflicts": ["<description>"]
}}
"""


def build_user_message(raw_text: str, project_name: str, n_images: int,
                       guidance: list[str] | None = None) -> str:
    guidance_block = ""
    if guidance:
        joined = "\n".join(f"- {g}" for g in guidance)
        guidance_block = (
            "\n## Red-print snippets detected from PDF character styles "
            "(form-filler guidance — ignore as data)\n\n" + joined + "\n"
        )
    return f"""\
Project name: {project_name}
Number of embedded images attached: {n_images}
{guidance_block}
## Extracted PDF text/tables

{raw_text}

Now output the analysis JSON object described in the system prompt:"""


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


def call_analysis_llm(user_message: str, image_paths: list[Path]) -> str:
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

    content: list = [{"type": "text", "text": user_message}]
    for img_path in image_paths:
        suffix = img_path.suffix.lower().lstrip(".")
        mime = {"jpg": "image/jpeg", "jpeg": "image/jpeg",
                "png": "image/png", "webp": "image/webp"}.get(suffix, "image/png")
        b64 = base64.b64encode(img_path.read_bytes()).decode()
        content.append({
            "type": "image_url",
            "image_url": {"url": f"data:{mime};base64,{b64}"},
        })

    use_responses = (
        api_mode == "responses"
        or (api_mode == "auto" and model.startswith("gpt-5"))
    )

    if use_responses:
        resp_input = [{"type": "input_text", "text": user_message}]
        for img_path in image_paths:
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
# Output validation (lightweight, mirrors understand.py's style)
# ---------------------------------------------------------------------------

def validate_analysis(analysis: dict) -> list[str]:
    warnings = []
    for key in ("file_name", "platform", "summary", "bios_version", "chips",
                "power_state", "susi_features", "gpio", "images_summary",
                "unresolved", "conflicts"):
        if key not in analysis:
            warnings.append(f"Missing top-level key: {key!r}")

    features = analysis.get("susi_features", {})
    for key in SUSI_FEATURE_KEYS:
        if key not in features:
            warnings.append(f"Missing susi_features entry: {key!r}")

    return warnings


def run_deep_analysis(raw: dict, image_paths: list[Path], project_name: str,
                      guidance: list[str] | None = None) -> dict:
    raw_text = raw_pages_to_text(raw)
    user_msg = build_user_message(raw_text, project_name, len(image_paths), guidance)

    print(f"Calling LLM ({os.environ.get('LLM_MODEL', 'gpt-5.3-codex')}) for deep analysis "
          f"({len(image_paths)} embedded image(s)) …", file=sys.stderr)
    raw_response = call_analysis_llm(user_msg, image_paths)

    try:
        analysis = json.loads(raw_response)
    except json.JSONDecodeError as e:
        sys.exit(f"LLM returned invalid JSON: {e}\n\nRaw output:\n{raw_response[:500]}")

    if not analysis.get("file_name"):
        analysis["file_name"] = f"{project_name}.pdf"
    if not analysis.get("platform"):
        analysis["platform"] = project_name

    for w in validate_analysis(analysis):
        print(f"Warning: {w}", file=sys.stderr)

    return analysis


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

def extract_pdf_to_json(pdf_path: Path, json_path: Path) -> Path:
    raw = extract_raw_pages(pdf_path)

    guidance = collect_form_guidance(pdf_path)
    if guidance:
        print(f"Detected {len(guidance)} red-print guidance snippet(s)", file=sys.stderr)

    images_dir = json_path.parent / "_extracted_images"
    image_paths = extract_embedded_images(pdf_path, images_dir, json_path.stem)

    analysis = run_deep_analysis(raw, image_paths, project_name=json_path.stem,
                                 guidance=guidance)

    # "form_guidance" is an additive top-level key ("pages" shape untouched):
    # understand.py forwards it to its own prompt as an ignore-list.
    result = {**raw, "form_guidance": guidance, "analysis": analysis}

    json_path.parent.mkdir(parents=True, exist_ok=True)
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=2)

    return json_path


def main():
    parser = argparse.ArgumentParser(
        description="Stage 0: PDF -> project JSON (raw text/tables + deep chip/GPIO/SUSI analysis)"
    )
    parser.add_argument("--project", help="Project name. Default filenames: <project>.pdf -> <project>.json")
    parser.add_argument("--in-pdf", dest="input_pdf", help="Input PDF path (overrides --project default)")
    parser.add_argument("--out-json", help="Output JSON path (overrides --project default)")
    parser.add_argument("--root", default=".", help="Base directory for relative paths (default: current directory)")
    args = parser.parse_args()

    root = Path(args.root).expanduser().resolve()
    pdf_path, json_path = resolve_paths(args.project, args.input_pdf, args.out_json, root)

    out = extract_pdf_to_json(pdf_path, json_path)
    print(f"Done: {out}")


if __name__ == "__main__":
    main()
