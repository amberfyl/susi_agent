#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Deterministic query tool for the active ``config_new.db`` schema.

The lookup key is always ``(product_name, chip_name)``.  ``ProductChip``
provides the corresponding ``hardware_id``; section rows are then selected by
that hardware id because the active schema no longer has ``prod_chip_id``
foreign keys in section tables.

This module intentionally does not read or fall back to the retired
``config.db`` schema.
"""

from __future__ import annotations

import argparse
import json
import re
import sqlite3
from pathlib import Path
from typing import Any


DEFAULT_DB_PATH = Path(__file__).with_name("config_new.db")
VALID_IDENTIFIER = re.compile(r"^[A-Za-z0-9_.]+$")
FAN_DEFAULT_CHIPS = {"EIO201", "EIO211", "IT8528", "IT5782", "IT5121"}


def quote_ident(name: str) -> str:
    """Quote a SQLite identifier after validating a table/column name."""
    if not VALID_IDENTIFIER.fullmatch(name):
        raise ValueError(f"Invalid identifier: {name}")
    return '"' + name.replace('"', '""') + '"'


def get_existing_tables(con: sqlite3.Connection) -> set[str]:
    rows = con.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
    ).fetchall()
    return {str(row[0]) for row in rows}


def norm_chip_name(chip_name: str) -> str:
    return re.sub(r"[^A-Za-z0-9]", "", (chip_name or "").upper())


def chip_uses_hwm_fan_defaults(chip_name: str) -> bool:
    """Compatibility helper used by the generator's fan-template path."""
    normalized = norm_chip_name(chip_name)
    return any(normalized.startswith(prefix) for prefix in FAN_DEFAULT_CHIPS)


def load_hwm_fan_defaults(con: sqlite3.Connection) -> dict[str, str]:
    """Return the common HWM.Fan template defaults in the new schema.

    ``config_new.db`` stores these values directly on ``HWM.Fan`` rows and no
    longer has ``HWM.Fan.Defaults``.  The current EC template values are
    stable across those rows, so this helper keeps the old Python import/API
    usable without querying any retired table.
    """
    defaults = {"io_port": "0", "options": "0x80000000", "pulses": "0"}
    if "HWM.Fan" not in get_existing_tables(con):
        return defaults

    row = con.execute(
        'SELECT io_port, options, pulses FROM "HWM.Fan" ORDER BY id LIMIT 1'
    ).fetchone()
    if row is not None:
        for key in defaults:
            value = str(row[key] or "").strip()
            if value:
                defaults[key] = value
    return defaults


def _normalize_row(row: sqlite3.Row) -> dict[str, Any]:
    """Return new-schema columns plus read-only compatibility aliases.

    The database source of truth remains ``report_name``, ``channel_id``,
    ``channel_name`` and ``options``.  The aliases let existing config-builder
    code consume the query result while it is being migrated from the retired
    schema; they are not database columns.
    """
    item = dict(row)
    item.setdefault("item_name", item.get("report_name", ""))
    item.setdefault("channel", item.get("channel_id", ""))
    item.setdefault("option", item.get("options", ""))
    item.setdefault("disp_name", item.get("channel_name", ""))
    return item


def _empty_result(product_name: str, chip_name: str, section: str) -> dict[str, Any]:
    return {
        "query_key": {
            "product_name": product_name,
            "chip_name": chip_name,
            "section": section,
        },
        "status": None,
        "prod_chip": None,
        "rows": [],
        "row_count": 0,
    }


def query_section(
    db_path: str | Path,
    product_name: str,
    chip_name: str,
    section: str,
) -> dict[str, Any]:
    """Query one section from ``config_new.db``.

    Status values:
      - ``DB_NOT_FOUND``
      - ``NO_SUCH_PRODUCT_CHIP``
      - ``SECTION_EMPTY``
      - ``FOUND``
      - ``INVALID_SECTION``
    """
    path = Path(db_path)
    result = _empty_result(product_name, chip_name, section)

    if not path.exists():
        result["status"] = "DB_NOT_FOUND"
        result["error"] = str(path)
        return result

    con = sqlite3.connect(str(path))
    con.row_factory = sqlite3.Row
    try:
        tables = get_existing_tables(con)
        if "ProductChip" not in tables:
            result["status"] = "INVALID_DATABASE"
            result["error"] = 'Missing required table "ProductChip"'
            return result
        if section not in tables or section == "ProductChip":
            result["status"] = "INVALID_SECTION"
            result["available_sections"] = sorted(t for t in tables if t != "ProductChip")
            return result

        prod = con.execute(
            """
            SELECT *
            FROM "ProductChip"
            WHERE product_name = ? AND chip_name = ?
            LIMIT 1
            """,
            (product_name, chip_name),
        ).fetchone()
        if prod is None:
            result["status"] = "NO_SUCH_PRODUCT_CHIP"
            return result

        result["prod_chip"] = dict(prod)
        hardware_id = str(prod["hardware_id"] or "").strip()
        result["lookup"] = {"hardware_id": hardware_id}

        table_name = quote_ident(section)
        rows = con.execute(
            f"SELECT * FROM {table_name} WHERE hardware_id = ? ORDER BY id",
            (hardware_id,),
        ).fetchall()

        result["rows"] = [_normalize_row(row) for row in rows]
        result["row_count"] = len(rows)
        result["status"] = "FOUND" if rows else "SECTION_EMPTY"
        return result
    finally:
        con.close()


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Query config_new.db by (product_name, chip_name, section)"
    )
    parser.add_argument("product_name", help="Product name key, e.g. AIMB")
    parser.add_argument("chip_name", help="Chip name key, e.g. EIO-201")
    parser.add_argument("section", help="Section table name, e.g. WDT / HWM.Fan")
    parser.add_argument(
        "--db",
        default=str(DEFAULT_DB_PATH),
        help="Path to config_new.db (default: %(default)s)",
    )
    args = parser.parse_args()

    result = query_section(
        db_path=args.db,
        product_name=args.product_name,
        chip_name=args.chip_name,
        section=args.section,
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))

    if result["status"] == "DB_NOT_FOUND":
        return 2
    if result["status"] in {"INVALID_DATABASE", "INVALID_SECTION"}:
        return 3
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
