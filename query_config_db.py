#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Deterministic query tool for config.db

Input key:
  - product_name
  - chip_name
  - section (target table)

Output statuses:
  - NO_SUCH_PRODUCT_CHIP
  - SECTION_EMPTY
  - FOUND
  - INVALID_SECTION
"""

from __future__ import annotations

import argparse
import json
import re
import sqlite3
from pathlib import Path


VALID_IDENTIFIER = re.compile(r"^[A-Za-z0-9_.]+$")
FAN_DEFAULT_CHIPS = {"EIO201", "EIO211", "IT8528", "IT5782", "IT5121"}


def quote_ident(name: str) -> str:
    if not VALID_IDENTIFIER.match(name):
        raise ValueError(f"Invalid identifier: {name}")
    return '"' + name.replace('"', '""') + '"'


def get_existing_tables(con: sqlite3.Connection) -> set[str]:
    rows = con.execute(
        "SELECT name FROM sqlite_master WHERE type='table'"
    ).fetchall()
    return {r[0] for r in rows}


def norm_chip_name(chip_name: str) -> str:
    return re.sub(r"[^A-Za-z0-9]", "", (chip_name or "").upper())


def chip_uses_hwm_fan_defaults(chip_name: str) -> bool:
    normalized = norm_chip_name(chip_name)
    return any(normalized.startswith(prefix) for prefix in FAN_DEFAULT_CHIPS)


def load_hwm_fan_defaults(con: sqlite3.Connection) -> dict[str, str]:
    defaults: dict[str, str] = {
        "io_port": "0",
        "options": "0x80000000",
        "pulses": "0",
    }

    tables = get_existing_tables(con)
    if "HWM.Fan.Defaults" not in tables:
        return defaults

    rows = con.execute(
        """
        SELECT name, value
        FROM "HWM.Fan.Defaults"
        ORDER BY id
        """
    ).fetchall()

    for r in rows:
        name = str(r[0] or "").strip()
        value = str(r[1] or "").strip()
        if not name:
            continue
        defaults[name] = value
    return defaults


def query_section(db_path: str | Path, product_name: str, chip_name: str, section: str) -> dict:
    db_path = Path(db_path)
    result: dict = {
        "query_key": {
            "product_name": product_name,
            "chip_name": chip_name,
            "section": section,
        },
        "status": None,
        "prod_chip": None,
        "rows": [],
    }

    if not db_path.exists():
        result["status"] = "DB_NOT_FOUND"
        result["error"] = str(db_path)
        return result

    con = sqlite3.connect(str(db_path))
    con.row_factory = sqlite3.Row
    try:
        tables = get_existing_tables(con)
        if section not in tables:
            result["status"] = "INVALID_SECTION"
            result["available_sections"] = sorted(t for t in tables if t != "ProductChip")
            return result

        prod = con.execute(
            """
            SELECT id, product_name, chip_name, hardware_id, config_chip
            FROM ProductChip
            WHERE product_name = ? AND chip_name = ?
            LIMIT 1
            """,
            (product_name, chip_name),
        ).fetchone()

        if prod is None:
            result["status"] = "NO_SUCH_PRODUCT_CHIP"
            return result

        prod_chip_id = prod["id"]
        result["prod_chip"] = dict(prod)

        table_name = quote_ident(section)

        # Section tables are not fully uniform (e.g., I2C has no item_name).
        # Build a compatible projection from available columns.
        tinfo = con.execute(f"PRAGMA table_info({table_name})").fetchall()
        colset = {str(r[1]) for r in tinfo}

        if "item_name" in colset:
            item_name_expr = '"item_name" AS "item_name"'
        elif section == "I2C":
            # I2C.id is a database primary key, not an INI channel number.
            item_name_expr = "'' AS \"item_name\""
        else:
            item_name_expr = "'' AS \"item_name\""

        channel_expr = '"channel" AS "channel"' if "channel" in colset else "'' AS \"channel\""
        io_port_expr = '"io_port" AS "io_port"' if "io_port" in colset else "'' AS \"io_port\""
        if "options" in colset:
            option_expr = '"options" AS "option"'
        elif "option" in colset:
            option_expr = '"option" AS "option"'
        else:
            option_expr = "'' AS \"option\""
        disp_name_expr = '"disp_name" AS "disp_name"' if "disp_name" in colset else "'' AS \"disp_name\""
        range_max_expr = '"range_max" AS "range_max"' if "range_max" in colset else "'' AS \"range_max\""
        range_min_expr = '"range_min" AS "range_min"' if "range_min" in colset else "'' AS \"range_min\""
        frequency_expr = '"frequency" AS "frequency"' if "frequency" in colset else "'' AS \"frequency\""

        rows = con.execute(
            f"""
            SELECT id,
                   {item_name_expr},
                   {channel_expr},
                   {io_port_expr},
                   {option_expr},
                   {range_max_expr},
                   {range_min_expr},
                   {frequency_expr},
                   {disp_name_expr}
            FROM {table_name}
            WHERE prod_chip_id = ?
            ORDER BY id
            """,
            (prod_chip_id,),
        ).fetchall()

        result["rows"] = [dict(r) for r in rows]
        result["row_count"] = len(rows)
        result["status"] = "FOUND" if rows else "SECTION_EMPTY"
        return result
    finally:
        con.close()


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Query /home/company2/AIagent_susi/config.db by (product_name, chip_name, section)"
    )
    parser.add_argument("product_name", help="Product name key, e.g. SOM")
    parser.add_argument("chip_name", help="Chip name key, e.g. EIO-211")
    parser.add_argument("section", help="Section table name, e.g. WDT / VGA.Backlight")
    parser.add_argument(
        "--db",
        default="/home/company2/AIagent_susi/config.db",
        help="Path to config.db",
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
    if result["status"] == "INVALID_SECTION":
        return 3
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
