#!/usr/bin/env python3
"""Build a database of U.S. ethnic (minority-owned) banks from FDIC data.

An "ethnic bank" here is an FDIC-insured Minority Depository Institution (MDI),
identified by the MDI_STATUS_CODE / MDI_STATUS_DESC fields of the FDIC BankFind
Suite `institutions` endpoint. Writes data/ethnic_banks.csv and
data/ethnic_banks.sqlite next to this script.

Usage:
    python3 build_db.py                         # fetch live from the FDIC API
    python3 build_db.py --from-file inst.csv    # build from FDIC API downloads (CSV or JSON)

Optional: set FDIC_API_KEY if the API asks for a key. Standard library only.
"""
import argparse
import csv
import io
import json
import os
import re
import sqlite3
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

API_BASES = ["https://api.fdic.gov/banks", "https://banks.data.fdic.gov/api"]
PAGE_SIZE = 10000
OUT_DIR = Path(__file__).resolve().parent / "data"

# FDIC Minority Status Codes (FIL-24-2022, attachment): codes 1-5 qualify on
# ownership (51%+ of voting stock held by minority individuals), codes 6-10 on a
# minority board majority serving a predominantly minority community. The
# letters B/H/A/N/M are the FDIC's group abbreviations.
GROUPS = [
    "Black or African American",
    "Hispanic American",
    "Asian or Pacific Islander American",
    "Native American or Alaska Native",
    "Multi-racial",
]
STATUS_CODES = {
    c: (GROUPS[(c - 1) % 5], "ownership" if c <= 5 else "board_and_community")
    for c in range(1, 11)
}
LETTER_CODES = dict(zip("BHANM", GROUPS))
MDI_FILTER = "MDI_STATUS_CODE:(" + " OR ".join(str(c) for c in STATUS_CODES) + ")"

# Checked in order against MDI_STATUS_DESC.
DESC_PATTERNS = [
    ("Multi-racial", re.compile(r"\bmulti", re.I)),
    ("Black or African American", re.compile(r"\b(black|african)\b", re.I)),
    ("Hispanic American", re.compile(r"\b(hispanic|latino)\b", re.I)),
    ("Asian or Pacific Islander American", re.compile(r"\b(asian|pacific)\b", re.I)),
    ("Native American or Alaska Native", re.compile(r"\b(native|alaska)", re.I)),
]

# (FDIC field, output column, type). Dollar amounts are in thousands.
FIELDS = [
    ("CERT", "cert", "int"),
    ("NAME", "name", "text"),
    ("MDI_STATUS_CODE", "mdi_status_code", "text"),
    ("MDI_STATUS_DESC", "mdi_status_desc", "text"),
    ("ACTIVE", "active", "int"),
    ("CITY", "city", "text"),
    ("STALP", "state", "text"),
    ("COUNTY", "county", "text"),
    ("ZIP", "zip", "zip"),
    ("ADDRESS", "address", "text"),
    ("CBSA", "cbsa", "text"),
    ("ASSET", "total_assets_thous", "int"),
    ("DEP", "total_deposits_thous", "int"),
    ("REPDTE", "financials_as_of", "date"),
    ("OFFICES", "offices", "int"),
    ("ESTYMD", "established", "date"),
    ("ENDEFYMD", "end_date", "date"),
    ("BKCLASS", "charter_class", "text"),
    ("CHRTAGNT", "chartering_agency", "text"),
    ("REGAGNT", "regulator", "text"),
    ("FED_RSSD", "fed_rssd", "int"),
    ("NAMEHCR", "holding_company", "text"),
    ("RSSDHCR", "holding_company_rssd", "int"),
    ("WEBADDR", "website", "text"),
]
COLUMNS = ["cert", "name", "ethnic_group", "mdi_basis"] + [
    col for _, col, _ in FIELDS if col not in ("cert", "name")
]
COLUMN_TYPES = {col: typ for _, col, typ in FIELDS}
COLUMN_TYPES.update(ethnic_group="text", mdi_basis="text")


def classify(code, desc):
    """Return (ethnic_group, mdi_basis, conflict) for an MDI, or None.

    The description text wins when it names a group; the numeric code fills
    gaps. `conflict` is True when the code and description disagree.
    """
    code = str(code if code is not None else "").strip()
    desc = str(desc or "")
    if re.fullmatch(r"\d+(\.0+)?", code):
        by_code = STATUS_CODES.get(int(float(code)))
    else:
        by_code = (LETTER_CODES[code.upper()], None) if code.upper() in LETTER_CODES else None
    by_desc = next((g for g, pat in DESC_PATTERNS if pat.search(desc)), None)
    if by_code is None and by_desc is None:
        return None
    group = by_desc or by_code[0]
    if re.search(r"\bboard\b", desc, re.I):
        basis = "board_and_community"
    elif re.search(r"\bown", desc, re.I):
        basis = "ownership"
    else:
        basis = by_code[1] if by_code else None
    conflict = bool(by_code and by_desc and by_code[0] != by_desc)
    return group, basis, conflict


def convert(value, typ):
    if value is None or str(value).strip() in ("", "null", "None"):
        return None
    value = str(value).strip()
    if typ == "int":
        try:
            return int(float(value))
        except ValueError:
            return None
    if typ == "zip":
        return value.zfill(5) if value.isdigit() else value
    if typ == "date":
        for fmt in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%d", "%m/%d/%Y", "%Y%m%d"):
            try:
                d = datetime.strptime(value, fmt)
            except ValueError:
                continue
            return None if d.year == 9999 else d.date().isoformat()
    return value


def get_json(url, attempts=3):
    req = urllib.request.Request(url, headers={"Accept": "application/json"})
    for i in range(attempts):
        try:
            with urllib.request.urlopen(req, timeout=120) as resp:
                return json.load(resp)
        except urllib.error.HTTPError as e:
            if e.code < 500 or i == attempts - 1:
                raise
        except urllib.error.URLError:
            if i == attempts - 1:
                raise
        time.sleep(2 ** (i + 1))


def fetch_institutions(filters, api_key=None):
    """All `institutions` records matching `filters`, trying each API base in turn."""
    errors = []
    for base in API_BASES:
        rows, offset = [], 0
        try:
            while True:
                params = {
                    "filters": filters,
                    "fields": ",".join(f for f, _, _ in FIELDS),
                    "limit": PAGE_SIZE,
                    "offset": offset,
                    "sort_by": "CERT",
                    "sort_order": "ASC",
                    "format": "json",
                }
                if api_key:
                    params["api_key"] = api_key
                payload = get_json(f"{base}/institutions?{urllib.parse.urlencode(params)}")
                batch = [r["data"] for r in payload["data"]]
                rows += batch
                offset += PAGE_SIZE
                total = payload.get("meta", {}).get("total")
                if not batch or (offset >= total if total is not None else len(batch) < PAGE_SIZE):
                    return rows, f"{base}/institutions"
        except (OSError, ValueError, KeyError) as e:
            errors.append(f"{base}: {e}")
    raise RuntimeError("Could not reach the FDIC API:\n  " + "\n  ".join(errors))


def load_files(paths):
    """Records from FDIC API responses saved as JSON, or CSV downloads."""
    rows = []
    for path in paths:
        text = Path(path).read_text(encoding="utf-8-sig")
        if text.lstrip().startswith(("{", "[")):
            payload = json.loads(text)
            items = payload["data"] if isinstance(payload, dict) else payload
            rows += [r.get("data", r) for r in items]
        else:
            rows += list(csv.DictReader(io.StringIO(text)))
    return rows


def build(rows):
    """Keep MDIs, one row per FDIC certificate. Returns (banks, warnings)."""
    banks, warnings = {}, []
    for raw in rows:
        r = {k.upper(): v for k, v in raw.items()}
        rec = {col: convert(r.get(f), typ) for f, col, typ in FIELDS}
        result = classify(rec["mdi_status_code"], rec["mdi_status_desc"])
        if result is None:
            if rec["mdi_status_code"] not in (None, "0", "0.0"):
                warnings.append(
                    f"cert {rec['cert']} ({rec['name']}): unrecognized MDI status "
                    f"{rec['mdi_status_code']!r} / {rec['mdi_status_desc']!r}; excluded"
                )
            continue
        rec["ethnic_group"], rec["mdi_basis"], conflict = result
        if conflict:
            warnings.append(
                f"cert {rec['cert']} ({rec['name']}): code {rec['mdi_status_code']} and "
                f"description {rec['mdi_status_desc']!r} disagree; used the description"
            )
        if rec["cert"] is None:
            warnings.append(f"{rec['name']}: no FDIC certificate number; excluded")
            continue
        banks[rec["cert"]] = rec
    order = sorted(banks.values(), key=lambda b: (b["ethnic_group"], b["state"] or "", b["name"] or ""))
    return order, warnings


def write_outputs(banks, info, out_dir):
    out_dir.mkdir(parents=True, exist_ok=True)
    with open(out_dir / "ethnic_banks.csv", "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=COLUMNS)
        writer.writeheader()
        writer.writerows({c: b[c] for c in COLUMNS} for b in banks)

    db_path = out_dir / "ethnic_banks.sqlite"
    db_path.unlink(missing_ok=True)
    sql_type = {"int": "INTEGER", "text": "TEXT", "zip": "TEXT", "date": "TEXT"}
    with sqlite3.connect(db_path) as con:
        cols = ", ".join(
            f"{c} {sql_type[COLUMN_TYPES[c]]}" + (" PRIMARY KEY" if c == "cert" else "")
            for c in COLUMNS
        )
        con.execute(f"CREATE TABLE banks ({cols})")
        con.executemany(
            f"INSERT INTO banks VALUES ({', '.join('?' for _ in COLUMNS)})",
            [[b[c] for c in COLUMNS] for b in banks],
        )
        con.execute("CREATE TABLE mdi_status_codes (code INTEGER PRIMARY KEY, ethnic_group TEXT, mdi_basis TEXT)")
        con.executemany(
            "INSERT INTO mdi_status_codes VALUES (?, ?, ?)",
            [(c, g, b) for c, (g, b) in STATUS_CODES.items()],
        )
        con.execute("CREATE TABLE build_info (key TEXT PRIMARY KEY, value TEXT)")
        con.executemany("INSERT INTO build_info VALUES (?, ?)", info.items())
        con.execute(
            """CREATE VIEW active_summary AS
               SELECT ethnic_group, mdi_basis, COUNT(*) AS banks,
                      SUM(total_assets_thous) AS total_assets_thous
               FROM banks WHERE active = 1
               GROUP BY ethnic_group, mdi_basis
               ORDER BY ethnic_group, mdi_basis"""
        )


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--from-file", nargs="+", metavar="PATH",
                        help="build from saved FDIC institutions data (JSON or CSV) instead of the API")
    parser.add_argument("--out-dir", type=Path, default=OUT_DIR)
    args = parser.parse_args(argv)

    fetch_warnings = []
    if args.from_file:
        rows, source = load_files(args.from_file), "file: " + ", ".join(Path(p).name for p in args.from_file)
    else:
        # The code filter finds MDIs open or closed; the full list of active
        # institutions is a safety net for status codes the filter misses.
        api_key = os.environ.get("FDIC_API_KEY")
        rows, sources = [], []
        for filters in (MDI_FILTER, "ACTIVE:1"):
            try:
                found, url = fetch_institutions(filters, api_key)
            except RuntimeError as e:
                fetch_warnings.append(f"query {filters!r} failed: {e}")
                continue
            rows += found
            sources.append(f"{url}?filters={filters}")
        if not sources:
            sys.exit("\n".join(fetch_warnings) + "\nDownload the data manually and rerun with --from-file (see README.md).")
        source = "; ".join(sources)

    banks, warnings = build(rows)
    warnings = fetch_warnings + warnings
    if not banks:
        sys.exit(f"No minority depository institutions found in {len(rows)} records from {source}.")
    info = {
        "source": source,
        "retrieved_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "records_scanned": str(len(rows)),
        "banks": str(len(banks)),
        "active_banks": str(sum(1 for b in banks if b["active"] == 1)),
        "warnings": json.dumps(warnings),
    }
    write_outputs(banks, info, args.out_dir)

    for w in warnings:
        print("warning:", w, file=sys.stderr)
    print(f"{len(banks)} MDIs ({info['active_banks']} active) from {source} -> {args.out_dir}")
    counts = {}
    for b in banks:
        if b["active"] == 1:
            counts[b["ethnic_group"]] = counts.get(b["ethnic_group"], 0) + 1
    for group, n in sorted(counts.items(), key=lambda kv: -kv[1]):
        print(f"  {n:4d}  {group}")


if __name__ == "__main__":
    main()
