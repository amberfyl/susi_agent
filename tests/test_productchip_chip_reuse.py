import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import query_config_db as q
import susi_gen as generator

FLAGS = ["SMBus", "I2C", "GPIO"]


def _make_db(path: Path, chip_rows):
    con = sqlite3.connect(str(path))
    cols = ", ".join(f'"{f}" TEXT NOT NULL DEFAULT "0"' for f in FLAGS)
    con.execute(
        "CREATE TABLE ProductChip (id INTEGER PRIMARY KEY, product_name TEXT NOT NULL, "
        f"chip_name TEXT NOT NULL, hardware_id TEXT NOT NULL, config_chip TEXT NOT NULL, {cols}, "
        "UNIQUE(product_name, chip_name))"
    )
    con.execute(
        'CREATE TABLE "GPIO" (id INTEGER PRIMARY KEY, hardware_id TEXT, report_name TEXT, '
        "channel_id TEXT, options TEXT)"
    )
    for r in chip_rows:
        con.execute(
            "INSERT INTO ProductChip (product_name, chip_name, hardware_id, config_chip, GPIO) "
            "VALUES (?,?,?,?,?)", r,
        )
    con.execute("INSERT INTO GPIO (hardware_id, report_name, channel_id, options) VALUES ('0xAA','GPIO00','0','0')")
    con.commit()
    con.close()


class ChipNameReuseTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.db = Path(self._tmp.name) / "t.db"
        _make_db(self.db, [
            ("MIO", "EIO-201", "0xAA", "Cfg A", "1"),
            ("AIMB", "EIO-201", "0xAA", "Cfg A", "0"),
            ("MIO", "EIO-X", "0xBB", "Cfg B", "1"),
            ("AIMB", "EIO-X", "0xCC", "Cfg C", "1"),
        ])

    def tearDown(self):
        self._tmp.cleanup()

    def test_exact_tuple_unchanged(self):
        r = q.query_section(self.db, "MIO", "EIO-201", "GPIO")
        self.assertEqual(r["status"], "FOUND")
        self.assertNotIn("source", r["lookup"])

    def test_new_product_reuses_hardware_id_by_chip_name(self):
        r = q.query_section(self.db, "SOM", "EIO-201", "GPIO")
        self.assertEqual(r["status"], "FOUND")
        self.assertEqual(r["row_count"], 1)
        self.assertEqual(r["lookup"]["hardware_id"], "0xAA")
        self.assertEqual(r["lookup"]["source"], "CHIP_NAME_REUSE")
        self.assertEqual(r["prod_chip"]["hardware_id"], "0xAA")
        self.assertEqual(r["prod_chip"]["config_chip"], "Cfg A")
        for flag in FLAGS:
            self.assertNotIn(flag, r["prod_chip"])

    def test_unknown_chip_still_missing(self):
        r = q.query_section(self.db, "SOM", "EIO-NONE", "GPIO")
        self.assertEqual(r["status"], "NO_SUCH_PRODUCT_CHIP")

    def test_conflicting_hardware_id_is_not_guessed(self):
        r = q.query_section(self.db, "SOM", "EIO-X", "GPIO")
        self.assertEqual(r["status"], "NO_SUCH_PRODUCT_CHIP")
        self.assertIn("EIO-X", r.get("error", ""))

    def test_generator_loader_reuses_by_chip_name(self):
        rec = generator._load_prod_chip_record(self.db, "SOM", "EIO-201")
        self.assertEqual(rec["hardware_id"], "0xAA")
        self.assertEqual(rec["product_name"], "SOM")
        self.assertEqual(rec["source"], "CHIP_NAME_REUSE")
        self.assertIsNone(generator._load_prod_chip_record(self.db, "SOM", "EIO-X"))
        self.assertIsNone(generator._load_prod_chip_record(self.db, "SOM", "EIO-NONE"))
        exact = generator._load_prod_chip_record(self.db, "MIO", "EIO-201")
        self.assertNotIn("source", exact)


if __name__ == "__main__":
    unittest.main()
