#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Propose and apply a ProductChip backfill after a Machine-B validation run.

A ProductChip row is ``(product_name, chip_name, hardware_id, config_chip, 13 flags)``.
``hardware_id``/``config_chip`` are reused from the same ``chip_name`` in the DB.
Each flag is the union over every project of that product series: it becomes
"1" once one project processed the section successfully, and is never lowered.

For one run, a flag is "1" when the spec processed the section and the final
summary verdict for it is PASS or CONDITIONAL.  A missing tuple is inserted;
an existing tuple only gets its "0" flags raised to "1".

The validator writes ``proposal.json`` after the final summary; the database
is written only by ``--apply`` after the user agreed.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from query_config_db import DEFAULT_DB_PATH, reuse_by_chip_name
from susi_gen import _infer_chip_name, _project_to_product_name

FLAG_COLUMNS = [
    "SMBus", "I2C", "VGA.Backlight", "VGA.Brightness", "HWM.Voltage", "HWM.Current",
    "HWM.Temperature", "HWM.Fan", "HWM.CaseOpen", "WDT", "GPIO", "StorageArea", "ThermalProtect",
]

# flag -> path of a boolean inside the spec JSON
SPEC_BOOL_PATHS: dict[str, tuple[str, ...]] = {
    "SMBus": ("features", "smbus"),
    "I2C": ("features", "i2c"),
    "VGA.Backlight": ("features", "backlight"),
    "VGA.Brightness": ("features", "brightness"),
    "WDT": ("features", "wdt"),
    "StorageArea": ("features", "storage"),
    "ThermalProtect": ("features", "thermalprotect"),
    "GPIO": ("feature_details", "gpio", "status"),
}

# flag -> (bios-image-cache hint field, spec list that also counts)
HWM_SOURCES: dict[str, tuple[str, str | None]] = {
    "HWM.Voltage": ("voltage_value_hints", None),
    "HWM.Temperature": ("temperature_value_hints", None),
    "HWM.Fan": ("fan_value_hints", None),
    "HWM.Current": ("current_value_hints", "currents"),
    "HWM.CaseOpen": ("caseopen_hints", "caseopen"),
}

SUCCESS_VERDICTS = {"PASS", "CONDITIONAL"}
PROPOSAL_DIRNAME = "productchip-backfill"


def _sha256(path: Path) -> str | None:
    if not path.exists():
        return None
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _load_json(path: Path) -> Any:
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def _dig(data: Any, path: tuple[str, ...]) -> Any:
    for key in path:
        if not isinstance(data, dict) or key not in data:
            return None
        data = data[key]
    return data


def _stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")


def spec_processed(spec: dict, cache: dict | None) -> tuple[dict[str, bool], list[str]]:
    """Return which flags the spec/BIOS evidence says were processed, plus warnings."""
    processed: dict[str, bool] = {}
    warnings: list[str] = []
    for col, path in SPEC_BOOL_PATHS.items():
        value = _dig(spec, path)
        if not isinstance(value, bool):
            warnings.append(f"{col}: spec {'.'.join(path)} is missing or not a boolean ({value!r}); counted as not processed")
        processed[col] = value is True
    items = cache.get("items") if isinstance(cache, dict) else None
    if cache is None:
        warnings.append("bios-image-cache.json is missing; HWM flags use the spec lists only")
    for col, (hint_field, spec_list) in HWM_SOURCES.items():
        cache_hit = any(bool(it.get(hint_field)) for it in items or [] if isinstance(it, dict))
        spec_hit = bool(spec.get(spec_list)) if spec_list else False
        processed[col] = cache_hit or spec_hit
    return processed, warnings


def build_proposal(
    project_dir: str | Path,
    run_id: str,
    db_path: str | Path = DEFAULT_DB_PATH,
    product_name: str | None = None,
    chip_name: str | None = None,
) -> dict[str, Any]:
    proj = Path(project_dir)
    project = proj.name
    run_dir = proj / "validation_runs" / run_id
    files = {
        "summary": run_dir / f"{project}-machineB-summary.json",
        "spec": proj / f"{project}-spec.json",
        "ini": proj / f"{project}-pre.ini",
        "bios_cache": proj / f"{project}-bios-image-cache.json",
    }
    proposal: dict[str, Any] = {
        "project": project, "run_id": run_id, "status": "BLOCKED", "action": None,
        "reasons": [], "warnings": [], "tuple": None,
        "run_flags": None, "existing_flags": None, "final_flags": None, "raised_flags": [],
        "flag_evidence": {},
        "inputs": {k: {"path": str(v), "sha256": _sha256(v)} for k, v in files.items()},
    }

    def stop(status: str, reason: str) -> dict[str, Any]:
        proposal["status"] = status
        proposal["reasons"].append(reason)
        return proposal

    for key in ("summary", "spec"):
        if not files[key].exists():
            return stop("NOT_ELIGIBLE", f"{key} file not found: {files[key]}")
    summary = _load_json(files["summary"])
    spec = _load_json(files["spec"])
    cache = _load_json(files["bios_cache"]) if files["bios_cache"].exists() else None

    if summary.get("project") != project or summary.get("run_id") != run_id:
        return stop("NOT_ELIGIBLE",
                    f"summary belongs to {summary.get('project')}/{summary.get('run_id')}, not {project}/{run_id}")
    written = files["summary"].stat().st_mtime
    for key in ("spec", "ini", "bios_cache"):
        p = files[key]
        if p.exists() and p.stat().st_mtime > written:
            return stop("NOT_ELIGIBLE", f"{p.name} was modified after the final summary was written")
    rollback = (summary.get("rollback") or {}).get("status")
    if rollback != "PASS":
        return stop("NOT_ELIGIBLE", f"rollback status is {rollback!r}, not PASS")
    fallback = summary.get("fallback")
    if isinstance(fallback, dict):
        recovery = fallback.get("final_runtime_recovery")
        if recovery != "PASS":
            return stop("NOT_ELIGIBLE", f"fallback recovery status is {recovery!r}, not PASS")
    sections = summary.get("sections") or []
    if not sections:
        return stop("NOT_ELIGIBLE", "summary has no section results")
    verdicts = {str(s.get("section")): str(s.get("verdict") or "").upper() for s in sections}

    processed, warnings = spec_processed(spec, cache)
    proposal["warnings"].extend(warnings)
    run_flags: dict[str, str] = {}
    for col in FLAG_COLUMNS:
        verdict = verdicts.get(col)
        ok = processed[col] and verdict in SUCCESS_VERDICTS
        run_flags[col] = "1" if ok else "0"
        proposal["flag_evidence"][col] = {"processed": processed[col], "verdict": verdict}
    proposal["run_flags"] = run_flags

    product = product_name or _project_to_product_name(project)
    chip = chip_name or _infer_chip_name(spec)
    if not chip:
        return stop("BLOCKED", "chip_name cannot be inferred from the spec")
    con = sqlite3.connect(f"file:{Path(db_path)}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    try:
        reused, reason = reuse_by_chip_name(con, product, chip)
        existing = con.execute(
            'SELECT * FROM "ProductChip" WHERE product_name = ? AND chip_name = ?', (product, chip)
        ).fetchone()
    finally:
        con.close()
    if reused is None:
        return stop("BLOCKED", reason or f"chip_name {chip} has no hardware_id in ProductChip")
    proposal["tuple"] = {
        "product_name": product, "chip_name": chip,
        "hardware_id": reused["hardware_id"], "config_chip": reused["config_chip"],
    }

    if existing is None:
        proposal["final_flags"] = dict(run_flags)
        proposal["raised_flags"] = [c for c in FLAG_COLUMNS if run_flags[c] == "1"]
        proposal["action"] = "INSERT"
        proposal["status"] = "READY"
        return proposal

    if (existing["hardware_id"], existing["config_chip"]) != (reused["hardware_id"], reused["config_chip"]):
        return stop("CONFLICT", f"existing tuple has hardware_id/config_chip "
                                f"{existing['hardware_id']}/{existing['config_chip']}")
    existing_flags = {c: str(existing[c]) for c in FLAG_COLUMNS}
    raised = [c for c in FLAG_COLUMNS if run_flags[c] == "1" and existing_flags[c] != "1"]
    proposal["existing_flags"] = existing_flags
    proposal["final_flags"] = {c: "1" if c in raised else existing_flags[c] for c in FLAG_COLUMNS}
    proposal["raised_flags"] = raised
    if not raised:
        proposal["status"] = "NO_OP"
        return proposal
    proposal["action"] = "UPDATE"
    proposal["status"] = "READY"
    return proposal


def describe(proposal: dict[str, Any]) -> str:
    """One line for the validation summary text."""
    t = proposal.get("tuple") or {}
    key = f"{t.get('product_name')}/{t.get('chip_name')}"
    status = proposal.get("status")
    if status == "READY":
        verb = "add new tuple" if proposal["action"] == "INSERT" else "raise flags on existing tuple"
        flags = ", ".join(proposal["raised_flags"]) or "(all flags 0)"
        return f"ProductChip backfill suggested: {verb} {key} (hardware_id {t.get('hardware_id')}); flags set to 1: {flags}"
    if status == "NO_OP":
        return f"ProductChip backfill: {key} already up to date"
    return f"ProductChip backfill not possible ({status}): {'; '.join(proposal.get('reasons') or [])}"


def write_proposal(project_dir: str | Path, run_id: str, db_path: str | Path = DEFAULT_DB_PATH) -> tuple[Path, dict[str, Any]]:
    proposal = build_proposal(project_dir, run_id, db_path)
    out_dir = Path(project_dir) / "validation_runs" / run_id / PROPOSAL_DIRNAME
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / "proposal.json"
    path.write_text(json.dumps(proposal, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return path, proposal


def _write_log(proposal: dict, outcome: dict, log_dir: Path) -> None:
    log_dir.mkdir(parents=True, exist_ok=True)
    stamp = _stamp()
    log_json = log_dir / f"apply-{stamp}.json"
    log_txt = log_dir / f"apply-{stamp}.txt"
    outcome["log_json"], outcome["log_txt"] = str(log_json), str(log_txt)
    log_json.write_text(json.dumps({"proposal": proposal, "outcome": outcome}, indent=2, ensure_ascii=False) + "\n",
                        encoding="utf-8")
    t = proposal.get("tuple") or {}
    lines = [
        f"ProductChip backfill: {outcome['status']}",
        f"project={proposal['project']} run_id={proposal['run_id']}",
        f"tuple={t.get('product_name')} / {t.get('chip_name')} / {t.get('hardware_id')} / {t.get('config_chip')}",
        f"action={outcome.get('action')} raised_flags={outcome.get('raised_flags')}",
    ]
    if outcome.get("reason"):
        lines.append(f"reason={outcome['reason']}")
    if outcome.get("backup_path"):
        lines.append(f"backup={outcome['backup_path']}")
    log_txt.write_text("\n".join(lines) + "\n", encoding="utf-8")


def apply_proposal(proposal: dict[str, Any], db_path: str | Path = DEFAULT_DB_PATH,
                   log_dir: str | Path | None = None) -> dict[str, Any]:
    """Write the proposal into ProductChip in one transaction (INSERT, or raise 0 -> 1)."""
    db = Path(db_path)
    run_dir = Path(proposal["inputs"]["summary"]["path"]).parent
    out_dir = Path(log_dir) if log_dir else run_dir / PROPOSAL_DIRNAME
    outcome: dict[str, Any] = {"status": "REFUSED", "reason": None, "action": None,
                               "raised_flags": [], "backup_path": None}

    def done(status: str, reason: str | None = None) -> dict[str, Any]:
        outcome["status"], outcome["reason"] = status, reason
        _write_log(proposal, outcome, out_dir)
        return outcome

    if proposal.get("status") != "READY":
        return done("REFUSED", f"proposal status is {proposal.get('status')}")
    t, run_flags = proposal["tuple"], proposal["run_flags"]
    key = (t["product_name"], t["chip_name"])

    out_dir.mkdir(parents=True, exist_ok=True)
    backup = out_dir / f"{db.stem}.before-backfill-{_stamp()}.bak"
    shutil.copy2(db, backup)
    outcome["backup_path"] = str(backup)

    con = sqlite3.connect(str(db), isolation_level=None)
    con.row_factory = sqlite3.Row
    try:
        con.execute("BEGIN IMMEDIATE")
        row = con.execute(
            'SELECT * FROM "ProductChip" WHERE product_name = ? AND chip_name = ?', key
        ).fetchone()
        if row is None:
            cols = ["product_name", "chip_name", "hardware_id", "config_chip", *FLAG_COLUMNS]
            values = [*key, t["hardware_id"], t["config_chip"], *[run_flags[c] for c in FLAG_COLUMNS]]
            con.execute(
                f'INSERT INTO "ProductChip" ({", ".join(chr(34) + c + chr(34) for c in cols)}) '
                f'VALUES ({", ".join("?" for _ in cols)})',
                values,
            )
            outcome["action"] = "INSERT"
            outcome["raised_flags"] = [c for c in FLAG_COLUMNS if run_flags[c] == "1"]
        else:
            if (row["hardware_id"], row["config_chip"]) != (t["hardware_id"], t["config_chip"]):
                con.execute("ROLLBACK")
                return done("CONFLICT", f"existing tuple has hardware_id/config_chip "
                                        f"{row['hardware_id']}/{row['config_chip']}")
            raised = [c for c in FLAG_COLUMNS if run_flags[c] == "1" and str(row[c]) != "1"]
            if not raised:
                con.execute("ROLLBACK")
                return done("NO_OP", "tuple already has every flag of this run")
            sets = ", ".join(f'"{c}" = ?' for c in raised)
            con.execute(f'UPDATE "ProductChip" SET {sets} WHERE product_name = ? AND chip_name = ?',
                        [run_flags[c] for c in raised] + list(key))
            outcome["action"] = "UPDATE"
            outcome["raised_flags"] = raised
        con.execute("COMMIT")
    except sqlite3.Error as exc:
        try:
            con.execute("ROLLBACK")
        except sqlite3.Error:
            pass
        return done("FAILED", f"{type(exc).__name__}: {exc}")
    finally:
        con.close()

    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    try:
        saved = con.execute(
            'SELECT * FROM "ProductChip" WHERE product_name = ? AND chip_name = ?', key
        ).fetchone()
    finally:
        con.close()
    if (saved is None
            or (saved["hardware_id"], saved["config_chip"]) != (t["hardware_id"], t["config_chip"])
            or any(str(saved[c]) != "1" for c in outcome["raised_flags"])):
        return done("FAILED", "read-back did not match the written tuple")
    return done("APPLIED")


def main() -> int:
    ap = argparse.ArgumentParser(description="Preview or apply a ProductChip backfill for one validation run.")
    ap.add_argument("--project-dir", required=True, help="CASES/<PROJECT> directory")
    ap.add_argument("--run-id", required=True, help="validation run id whose final summary is used")
    ap.add_argument("--db", default=str(DEFAULT_DB_PATH))
    ap.add_argument("--apply", action="store_true", help="write ProductChip (default: preview only)")
    args = ap.parse_args()

    path, proposal = write_proposal(args.project_dir, args.run_id, args.db)
    print(describe(proposal))
    print(f"Proposal: {path}")
    if not args.apply:
        return 0 if proposal["status"] in ("READY", "NO_OP") else 2
    outcome = apply_proposal(proposal, args.db)
    print(json.dumps(outcome, indent=2, ensure_ascii=False))
    return 0 if outcome["status"] in ("APPLIED", "NO_OP") else 1


if __name__ == "__main__":
    sys.exit(main())
