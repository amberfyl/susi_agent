# -*- coding: utf-8 -*-
"""
build_chip_db.py
Scan all chip XMLs under SusiEditor Config/ and build chip_db.db (SQLite).

Run once (or when XMLs are updated):
  python3 build_chip_db.py [--config-dir <path>] [--out chip_db.db]

Schema:
  chips(id, name)
  categories(id, chip_id, category, hwid)
  supports(id, category_id, entry)        -- raw "name,code" or "group,bit"
  ports(id, category_id, address)         -- I2C device addresses
"""

import argparse
import sqlite3
import xml.etree.ElementTree as ET
from pathlib import Path


DDL = """
CREATE TABLE IF NOT EXISTS chips (
    id   INTEGER PRIMARY KEY,
    name TEXT    UNIQUE NOT NULL
);
CREATE TABLE IF NOT EXISTS categories (
    id       INTEGER PRIMARY KEY,
    chip_id  INTEGER NOT NULL REFERENCES chips(id),
    category TEXT    NOT NULL,
    hwid     TEXT    NOT NULL,
    UNIQUE(chip_id, category)
);
CREATE TABLE IF NOT EXISTS supports (
    id          INTEGER PRIMARY KEY,
    category_id INTEGER NOT NULL REFERENCES categories(id),
    entry       TEXT    NOT NULL
);
CREATE TABLE IF NOT EXISTS ports (
    id          INTEGER PRIMARY KEY,
    category_id INTEGER NOT NULL REFERENCES categories(id),
    address     TEXT    NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_cat_chip ON categories(chip_id);
CREATE INDEX IF NOT EXISTS idx_sup_cat  ON supports(category_id);
CREATE INDEX IF NOT EXISTS idx_port_cat ON ports(category_id);
"""


def _find_config_dir(script_dir: Path) -> Path | None:
    for candidate in [
        script_dir / "00_SusiEditor" / "00_SusiEditor" / "Config",
        script_dir.parent / "00_SusiEditor" / "00_SusiEditor" / "Config",
    ]:
        if candidate.exists():
            return candidate
    return None


def parse_chip_xml(path: Path) -> dict | None:
    """Parse a chip XML. Returns dict or None if no HWID (Config.xml schema files)."""
    try:
        tree = ET.parse(str(path))
    except ET.ParseError as e:
        print(f"  [warn] {path.name}: {e}")
        return None

    root = tree.getroot()
    hwid = (root.findtext("HWID") or "").strip()
    if not hwid:
        return None

    support = [
        s.text.strip()
        for s in root.iter("Support")
        if s.text and s.text.strip()
    ]
    ports = [
        p.text.strip()
        for p in root.iter("Port")
        if p.text and p.text.strip()
    ]
    return {"hwid": hwid, "support": support, "ports": ports}


def build_db(config_dir: Path, out_path: Path) -> tuple[int, int]:
    if out_path.exists():
        out_path.unlink()

    con = sqlite3.connect(str(out_path))
    con.executescript(DDL)

    for xml_path in sorted(config_dir.rglob("*.xml")):
        if xml_path.name == "Config.xml":
            continue

        data = parse_chip_xml(xml_path)
        if data is None:
            continue

        chip_stem = xml_path.stem
        category  = "/".join(xml_path.relative_to(config_dir).parts[:-1])

        con.execute("INSERT OR IGNORE INTO chips(name) VALUES(?)", (chip_stem,))
        chip_id = con.execute(
            "SELECT id FROM chips WHERE name=?", (chip_stem,)
        ).fetchone()[0]

        con.execute(
            "INSERT OR REPLACE INTO categories(chip_id, category, hwid) VALUES(?,?,?)",
            (chip_id, category, data["hwid"]),
        )
        cat_id = con.execute(
            "SELECT id FROM categories WHERE chip_id=? AND category=?",
            (chip_id, category),
        ).fetchone()[0]

        con.executemany(
            "INSERT INTO supports(category_id, entry) VALUES(?,?)",
            [(cat_id, e) for e in data["support"]],
        )
        con.executemany(
            "INSERT INTO ports(category_id, address) VALUES(?,?)",
            [(cat_id, p) for p in data["ports"]],
        )

    con.commit()

    chip_count = con.execute("SELECT COUNT(*) FROM chips").fetchone()[0]
    cat_count  = con.execute("SELECT COUNT(*) FROM categories").fetchone()[0]
    con.close()
    return chip_count, cat_count


def main():
    parser = argparse.ArgumentParser(
        description="Build chip_db.db (SQLite) from SusiEditor Config XMLs."
    )
    parser.add_argument("--config-dir", default=None,
                        help="Path to Config/ directory (auto-detected if omitted)")
    parser.add_argument("--out", default=None,
                        help="Output .db path (default: chip_db.db beside this script)")
    args = parser.parse_args()

    script_dir = Path(__file__).parent.resolve()

    config_dir = (
        Path(args.config_dir).resolve() if args.config_dir
        else _find_config_dir(script_dir)
    )
    if config_dir is None:
        raise SystemExit("Cannot locate Config/ directory. Use --config-dir.")

    out_path = Path(args.out).resolve() if args.out else script_dir / "chip_db.db"

    print(f"Scanning : {config_dir}")
    chips, cats = build_db(config_dir, out_path)
    print(f"Written  : {out_path}")
    print(f"           {chips} chips, {cats} category entries")

    con = sqlite3.connect(str(out_path))
    print("\nCategory summary:")
    rows = con.execute("""
        SELECT category, COUNT(*) AS chip_cnt
        FROM categories
        GROUP BY category
        ORDER BY category
    """).fetchall()
    for cat, cnt in rows:
        print(f"  {cat:35s} {cnt} chips")
    con.close()


if __name__ == "__main__":
    main()
