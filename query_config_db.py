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


def quote_ident(name: str) -> str:
    if not VALID_IDENTIFIER.match(name):
        raise ValueError(f"Invalid identifier: {name}")
    return '"' + name.replace('"', '""') + '"'


def get_existing_tables(con: sqlite3.Connection) -> set[str]:
    rows = con.execute(
        "SELECT name FROM sqlite_master WHERE type='table'"
    ).fetchall()
    return {r[0] for r in rows}


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
        rows = con.execute(
            f"""
            SELECT id, item_name, channel, io_port, options AS option, disp_name
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
