import json
import os
import sqlite3
import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import productchip_backfill as bf
import query_config_db as q

FLAG_COLS = bf.FLAG_COLUMNS


def make_db(path: Path):
    con = sqlite3.connect(str(path))
    cols = ", ".join(f'"{c}" TEXT NOT NULL DEFAULT "0" CHECK ("{c}" IN ("0","1"))' for c in FLAG_COLS)
    con.execute(
        "CREATE TABLE ProductChip (id INTEGER PRIMARY KEY, product_name TEXT NOT NULL, chip_name TEXT NOT NULL, "
        f"hardware_id TEXT NOT NULL, config_chip TEXT NOT NULL, {cols}, UNIQUE(product_name, chip_name))"
    )
    con.execute('CREATE TABLE "GPIO" (id INTEGER PRIMARY KEY, hardware_id TEXT, report_name TEXT)')
    con.execute("INSERT INTO ProductChip (product_name, chip_name, hardware_id, config_chip, WDT) "
                "VALUES ('MIO','EIO-201','0xAA','Cfg A','1')")
    con.execute("INSERT INTO GPIO (hardware_id, report_name) VALUES ('0xAA','GPIO00')")
    con.commit()
    con.close()


SPEC = {
    "project": "SOM-9999",
    "chips": {"ec": "EIO-201"},
    "features": {"smbus": True, "i2c": False, "backlight": False, "brightness": True,
                 "wdt": True, "storage": True, "thermalprotect": True},
    "feature_details": {"gpio": {"status": True}},
    "currents": [], "caseopen": [],
}
CACHE = {"items": [{
    "voltage_value_hints": [{"label": "VBAT"}], "temperature_value_hints": [{"label": "CPU"}],
    "fan_value_hints": [], "current_value_hints": [], "caseopen_hints": [],
}]}
SECTIONS = [
    {"section": "GPIO", "verdict": "PASS"},
    {"section": "SMBus", "verdict": "CONDITIONAL"},
    {"section": "I2C", "verdict": "PASS"},             # spec false -> stays 0
    {"section": "WDT", "verdict": "PASS"},
    {"section": "StorageArea", "verdict": "FAIL"},     # FAIL -> 0
    {"section": "HWM.Voltage", "verdict": "PASS"},
    {"section": "HWM.Temperature", "verdict": "ERROR"},
    {"section": "VGA.Brightness", "verdict": "PASS"},
    # ThermalProtect not run -> 0
]


def summary(project="SOM-9999", run_id="r1", rollback="PASS", sections=None, fallback=None):
    s = {"project": project, "run_id": run_id, "status": "SECTION_FAIL",
         "rollback": {"status": rollback}, "sections": SECTIONS if sections is None else sections}
    if fallback is not None:
        s["fallback"] = fallback
    return s


class Base(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        root = Path(self._tmp.name)
        self.db = root / "c.db"
        make_db(self.db)
        self.proj = root / "CASES" / "SOM-9999"
        (self.proj / "validation_runs" / "r1").mkdir(parents=True)
        self.write_all()

    def tearDown(self):
        self._tmp.cleanup()

    def write_all(self, spec=SPEC, cache=CACHE, summ=None):
        (self.proj / "SOM-9999-spec.json").write_text(json.dumps(spec), encoding="utf-8")
        cache_path = self.proj / "SOM-9999-bios-image-cache.json"
        if cache is None:
            cache_path.unlink(missing_ok=True)
        else:
            cache_path.write_text(json.dumps(cache), encoding="utf-8")
        (self.proj / "SOM-9999-pre.ini").write_text("[GPIO]\n", encoding="utf-8")
        s = self.proj / "validation_runs" / "r1" / "SOM-9999-machineB-summary.json"
        s.write_text(json.dumps(summ or summary()), encoding="utf-8")
        later = time.time() + 5
        os.utime(s, (later, later))

    def propose(self, **kw):
        return bf.build_proposal(self.proj, "r1", self.db, **kw)


class FlagTests(Base):
    def test_flag_is_one_only_when_spec_processed_and_section_succeeded(self):
        p = self.propose()
        self.assertEqual((p["status"], p["action"]), ("READY", "INSERT"), p)
        t = p["tuple"]
        self.assertEqual((t["product_name"], t["chip_name"], t["hardware_id"], t["config_chip"]),
                         ("SOM", "EIO-201", "0xAA", "Cfg A"))
        f = p["run_flags"]
        expect_one = {"GPIO", "SMBus", "WDT", "HWM.Voltage", "VGA.Brightness"}
        for col in FLAG_COLS:
            self.assertEqual(f[col], "1" if col in expect_one else "0", col)

    def test_current_caseopen_from_cache_or_spec_list(self):
        spec = dict(SPEC, currents=[{"ini_key": "OEM0"}])
        cache = {"items": [dict(CACHE["items"][0], caseopen_hints=[{"label": "Case", "state": "closed"}])]}
        sections = SECTIONS + [{"section": "HWM.Current", "verdict": "PASS"},
                               {"section": "HWM.CaseOpen", "verdict": "PASS"}]
        self.write_all(spec=spec, cache=cache, summ=summary(sections=sections))
        f = self.propose()["run_flags"]
        self.assertEqual((f["HWM.Current"], f["HWM.CaseOpen"]), ("1", "1"))

    def test_missing_spec_field_counts_as_not_processed_with_warning(self):
        spec = json.loads(json.dumps(SPEC))
        spec["features"]["wdt"] = "unknown"
        self.write_all(spec=spec)
        p = self.propose()
        self.assertEqual(p["status"], "READY")
        self.assertEqual(p["run_flags"]["WDT"], "0")
        self.assertTrue(any("WDT" in w for w in p["warnings"]))

    def test_missing_bios_cache_only_warns(self):
        self.write_all(cache=None)
        p = self.propose()
        self.assertEqual(p["status"], "READY")
        self.assertEqual(p["run_flags"]["HWM.Voltage"], "0")
        self.assertTrue(any("bios-image-cache" in w for w in p["warnings"]))


class ExistingTupleTests(Base):
    def test_existing_tuple_only_raises_zero_to_one(self):
        p = self.propose(product_name="MIO")
        self.assertEqual((p["status"], p["action"]), ("READY", "UPDATE"))
        self.assertNotIn("WDT", p["raised_flags"])  # already 1
        self.assertIn("GPIO", p["raised_flags"])
        self.assertEqual(p["final_flags"]["WDT"], "1")

    def test_existing_one_is_never_lowered(self):
        con = sqlite3.connect(str(self.db))
        con.execute('UPDATE ProductChip SET "I2C"="1", "StorageArea"="1"')
        con.commit(); con.close()
        p = self.propose(product_name="MIO")
        self.assertEqual((p["final_flags"]["I2C"], p["final_flags"]["StorageArea"]), ("1", "1"))

    def test_nothing_new_is_noop(self):
        con = sqlite3.connect(str(self.db))
        con.execute('UPDATE ProductChip SET ' + ", ".join(f'"{c}"="1"' for c in FLAG_COLS))
        con.commit(); con.close()
        self.assertEqual(self.propose(product_name="MIO")["status"], "NO_OP")


class EligibilityTests(Base):
    def test_rollback_not_pass_blocks(self):
        self.write_all(summ=summary(rollback="FAIL"))
        self.assertEqual(self.propose()["status"], "NOT_ELIGIBLE")

    def test_fallback_recovery_not_pass_blocks(self):
        self.write_all(summ=summary(fallback={"final_runtime_recovery": "FAIL"}))
        self.assertEqual(self.propose()["status"], "NOT_ELIGIBLE")
        self.write_all(summ=summary(fallback={"final_runtime_recovery": "PASS"}))
        self.assertEqual(self.propose()["status"], "READY")

    def test_section_fail_does_not_block_the_tuple(self):
        sections = [{"section": "GPIO", "verdict": "FAIL"}]
        self.write_all(summ=summary(sections=sections))
        p = self.propose()
        self.assertEqual(p["status"], "READY")
        self.assertEqual(p["run_flags"]["GPIO"], "0")

    def test_project_or_run_mismatch_blocks(self):
        self.write_all(summ=summary(project="OTHER"))
        self.assertEqual(self.propose()["status"], "NOT_ELIGIBLE")
        self.write_all(summ=summary(run_id="r2"))
        self.assertEqual(self.propose()["status"], "NOT_ELIGIBLE")

    def test_input_modified_after_final_summary_blocks(self):
        spec = self.proj / "SOM-9999-spec.json"
        later = time.time() + 60
        os.utime(spec, (later, later))
        self.assertEqual(self.propose()["status"], "NOT_ELIGIBLE")

    def test_unknown_chip_blocks(self):
        self.write_all(spec=dict(SPEC, chips={"ec": "EIO-999"}))
        self.assertEqual(self.propose()["status"], "BLOCKED")

    def test_write_proposal_and_describe(self):
        path, p = bf.write_proposal(self.proj, "r1", self.db)
        self.assertTrue(path.is_file())
        self.assertEqual(json.loads(path.read_text(encoding="utf-8"))["status"], "READY")
        self.assertIn("SOM/EIO-201", bf.describe(p))


class ApplyTests(Base):
    def rows(self):
        con = sqlite3.connect(str(self.db))
        n = (con.execute("select count(*) from ProductChip").fetchone()[0],
             con.execute("select count(*) from GPIO").fetchone()[0])
        con.close()
        return n

    def test_apply_insert_and_query_finds_it(self):
        out = bf.apply_proposal(self.propose(), self.db)
        self.assertEqual((out["status"], out["action"]), ("APPLIED", "INSERT"), out)
        self.assertEqual(self.rows(), (2, 1))
        r = q.query_section(self.db, "SOM", "EIO-201", "GPIO")
        self.assertEqual(r["status"], "FOUND")
        self.assertNotIn("source", r["lookup"])
        self.assertEqual((r["prod_chip"]["GPIO"], r["prod_chip"]["I2C"]), ("1", "0"))
        self.assertTrue(Path(out["backup_path"]).exists())
        self.assertTrue(Path(out["log_json"]).exists())

    def test_apply_update_raises_only_new_flags(self):
        out = bf.apply_proposal(self.propose(product_name="MIO"), self.db)
        self.assertEqual((out["status"], out["action"]), ("APPLIED", "UPDATE"), out)
        self.assertEqual(self.rows(), (1, 1))
        con = sqlite3.connect(str(self.db)); con.row_factory = sqlite3.Row
        row = con.execute("select * from ProductChip").fetchone(); con.close()
        self.assertEqual((row["GPIO"], row["WDT"], row["I2C"]), ("1", "1", "0"))

    def test_apply_twice_is_noop(self):
        p = self.propose()
        bf.apply_proposal(p, self.db)
        self.assertEqual(bf.apply_proposal(p, self.db)["status"], "NO_OP")
        self.assertEqual(self.rows(), (2, 1))

    def test_apply_refuses_non_ready(self):
        self.write_all(summ=summary(rollback="FAIL"))
        self.assertEqual(bf.apply_proposal(self.propose(), self.db)["status"], "REFUSED")
        self.assertEqual(self.rows(), (1, 1))

    def test_conflicting_tuple_not_overwritten(self):
        p = self.propose()
        con = sqlite3.connect(str(self.db))
        con.execute("INSERT INTO ProductChip (product_name, chip_name, hardware_id, config_chip) "
                    "VALUES ('SOM','EIO-201','0xFF','Other')")
        con.commit(); con.close()
        self.assertEqual(bf.apply_proposal(p, self.db)["status"], "CONFLICT")
        self.assertEqual(self.rows(), (2, 1))

    def test_sql_failure_leaves_no_partial_row(self):
        p = self.propose()
        p["run_flags"]["SMBus"] = "2"  # violates CHECK
        self.assertEqual(bf.apply_proposal(p, self.db)["status"], "FAILED")
        self.assertEqual(self.rows(), (1, 1))


class ValidatorHookTests(Base):
    def contract(self):
        from types import SimpleNamespace
        run_root = self.proj / "validation_runs" / "r1"
        text = run_root / "SOM-9999-machineB-summary.txt"
        text.write_text("Summary\n", encoding="utf-8")
        return SimpleNamespace(
            project="SOM-9999", run_id="r1",
            inputs=SimpleNamespace(case_dir=self.proj),
            outputs=SimpleNamespace(run_root=run_root, summary_text=text),
        )

    def test_hook_writes_proposal_and_summary_line(self):
        from run_machineB_full_validation import write_productchip_backfill_proposal
        c = self.contract()
        path = write_productchip_backfill_proposal(c, self.db)
        self.assertEqual(path, c.outputs.run_root / "productchip-backfill" / "proposal.json")
        text = c.outputs.summary_text.read_text(encoding="utf-8")
        self.assertIn("ProductChip backfill suggested: add new tuple SOM/EIO-201", text)
        self.assertIn("proposal: productchip-backfill/proposal.json -- ask the user before applying", text)
        self.assertEqual(self.rows_in_db(), 1)  # the hook never writes the DB

    def test_hook_error_is_reported_not_raised(self):
        from run_machineB_full_validation import _report_backfill_proposal
        c = self.contract()
        _report_backfill_proposal(c, str(Path(self._tmp.name) / "no-such-repo"))

    def rows_in_db(self):
        con = sqlite3.connect(str(self.db))
        n = con.execute("select count(*) from ProductChip").fetchone()[0]
        con.close()
        return n


if __name__ == "__main__":
    unittest.main()
