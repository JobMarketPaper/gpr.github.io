"""Offline tests for build_db.py. Records below are synthetic, shaped like FDIC API output."""
import csv
import json
import sqlite3
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest import mock

import build_db


def api_record(cert, name, code, desc, active=1, **extra):
    data = {"CERT": cert, "NAME": name, "MDI_STATUS_CODE": code, "MDI_STATUS_DESC": desc,
            "ACTIVE": active, "STALP": "CA", "ASSET": 1000, "ZIP": "2110",
            "ESTYMD": "03/01/1990", "ENDEFYMD": "12/31/9999", "REPDTE": "20240630"}
    data.update(extra)
    return {"data": data, "score": 0}


RECORDS = [
    api_record(1, "Test Bank One", 3, "Asian or Pacific Islander American owned"),
    api_record(2, "Test Bank Two", 6, "Minority board and Black or African American community served"),
    api_record(3, "Test Bank Three", 0, "Not a minority depository institution"),
    api_record(4, "Test Bank Four", 2, None, active=0, ENDEFYMD="06/30/2015"),
    api_record(1, "Test Bank One", 3, "Asian or Pacific Islander American owned"),  # duplicate cert
    api_record(5, "Test Bank Five", 99, "Something new"),
]


class ClassifyTest(unittest.TestCase):
    def test_codes(self):
        self.assertEqual(build_db.classify(1, None), ("Black or African American", "ownership", False))
        self.assertEqual(build_db.classify("8", ""), ("Asian or Pacific Islander American", "board_and_community", False))
        self.assertEqual(build_db.classify("10.0", None), ("Multi-racial", "board_and_community", False))
        self.assertEqual(build_db.classify("N", None), ("Native American or Alaska Native", None, False))

    def test_non_mdi(self):
        for code, desc in [(0, None), ("", ""), (None, None), (0, "Not a minority depository institution"),
                           (0, "Caucasian owned")]:
            self.assertIsNone(build_db.classify(code, desc), (code, desc))

    def test_description(self):
        self.assertEqual(build_db.classify(None, "Hispanic American owned"),
                         ("Hispanic American", "ownership", False))
        self.assertEqual(build_db.classify(4, "Native American or Alaskan Native American owned"),
                         ("Native American or Alaska Native", "ownership", False))
        self.assertEqual(build_db.classify(5, "Multi-racial American owned"),
                         ("Multi-racial", "ownership", False))

    def test_conflict_prefers_description(self):
        self.assertEqual(build_db.classify(1, "Hispanic American owned"),
                         ("Hispanic American", "ownership", True))


class ConvertTest(unittest.TestCase):
    def test_values(self):
        self.assertEqual(build_db.convert("12345.0", "int"), 12345)
        self.assertEqual(build_db.convert("2110", "zip"), "02110")
        self.assertEqual(build_db.convert("03/01/1990", "date"), "1990-03-01")
        self.assertEqual(build_db.convert("20240630", "date"), "2024-06-30")
        self.assertEqual(build_db.convert("1990-03-01T00:00:00", "date"), "1990-03-01")
        self.assertIsNone(build_db.convert("12/31/9999", "date"))
        self.assertIsNone(build_db.convert("", "text"))


class BuildTest(unittest.TestCase):
    def test_build(self):
        banks, warnings = build_db.build([r["data"] for r in RECORDS])
        self.assertEqual([b["cert"] for b in banks], [1, 2, 4])
        by_cert = {b["cert"]: b for b in banks}
        self.assertEqual(by_cert[2]["mdi_basis"], "board_and_community")
        self.assertEqual(by_cert[4]["end_date"], "2015-06-30")
        self.assertEqual(by_cert[1]["zip"], "02110")
        self.assertEqual(len(warnings), 1)
        self.assertIn("cert 5", warnings[0])

    def test_main_from_json_and_csv(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            src = tmp / "inst.json"
            src.write_text(json.dumps({"meta": {"total": len(RECORDS)}, "data": RECORDS}))
            build_db.main(["--from-file", str(src), "--out-dir", str(tmp / "out")])

            with open(tmp / "out" / "ethnic_banks.csv", newline="") as f:
                rows = list(csv.DictReader(f))
            self.assertEqual(list(rows[0]), build_db.COLUMNS)
            self.assertEqual(len(rows), 3)

            con = sqlite3.connect(tmp / "out" / "ethnic_banks.sqlite")
            self.assertEqual(con.execute("SELECT COUNT(*) FROM banks").fetchone()[0], 3)
            self.assertEqual(con.execute("SELECT COUNT(*) FROM mdi_status_codes").fetchone()[0], 10)
            summary = con.execute("SELECT ethnic_group, banks FROM active_summary").fetchall()
            self.assertEqual(summary, [("Asian or Pacific Islander American", 1), ("Black or African American", 1)])
            info = dict(con.execute("SELECT key, value FROM build_info"))
            self.assertEqual(info["banks"], "3")
            self.assertEqual(len(json.loads(info["warnings"])), 1)
            con.close()

            # A CSV download with FDIC field names builds the same table.
            fdic_csv = tmp / "inst.csv"
            with open(fdic_csv, "w", newline="") as f:
                writer = csv.DictWriter(f, fieldnames=list(RECORDS[0]["data"]))
                writer.writeheader()
                writer.writerows(r["data"] for r in RECORDS)
            build_db.main(["--from-file", str(fdic_csv), "--out-dir", str(tmp / "out2")])
            self.assertEqual((tmp / "out" / "ethnic_banks.csv").read_text(),
                             (tmp / "out2" / "ethnic_banks.csv").read_text())


class FetchTest(unittest.TestCase):
    def test_pagination_and_fallback(self):
        calls = []

        def fake_get_json(url):
            calls.append(url)
            if url.startswith(build_db.API_BASES[0]):
                raise urllib.error.HTTPError(url, 403, "Forbidden", {}, None)
            offset = int(url.split("offset=")[1].split("&")[0])
            page = RECORDS[offset:offset + 2]
            return {"meta": {"total": len(RECORDS)}, "data": page}

        with mock.patch.object(build_db, "PAGE_SIZE", 2), mock.patch.object(build_db, "get_json", fake_get_json):
            rows, url = build_db.fetch_institutions("ACTIVE:1")
        self.assertEqual(url, build_db.API_BASES[1] + "/institutions")
        self.assertEqual(len(rows), len(RECORDS))
        self.assertEqual(len(calls), 1 + 3)  # one failed call on the first base, three pages on the second

    def test_main_survives_one_failed_query(self):
        def fake_fetch(filters, api_key=None):
            if filters == build_db.MDI_FILTER:
                raise RuntimeError("bad filter")
            return [r["data"] for r in RECORDS], "https://example.test/institutions"

        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(build_db, "fetch_institutions", fake_fetch):
            build_db.main(["--out-dir", tmp])
            con = sqlite3.connect(Path(tmp) / "ethnic_banks.sqlite")
            info = dict(con.execute("SELECT key, value FROM build_info"))
            con.close()
        self.assertIn("bad filter", info["warnings"])
        self.assertEqual(info["banks"], "3")


if __name__ == "__main__":
    unittest.main()
