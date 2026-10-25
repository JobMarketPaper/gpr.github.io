"""
link_borrowers.py
Ethnicity of DealScan borrowers from Revelio, and co-ethnic measures for the ethnic banks' syndicated loans.

1. Borrowers: the borrowers in the ethnic banks' loans (data/bank_dealscan_loans.csv, from link_dealscan.py),
   or with --all-us every U.S. borrower in DealScan, as a baseline for comparing ethnic banks with other lenders.
2. Match borrowers to U.S. Revelio companies (revelio_common.company_mapping) with link_revelio.match minus
   the banking restriction: standardized name + HQ city, then standardized name alone, unique on both sides.
   --company-mapping reuses a local parquet copy of company_mapping (e.g. from 02_revelio_company_map.py)
   instead of pulling the U.S. companies from WRDS.
3. Borrower-year ethnicity on December 31 (link_revelio.annual_shares): executives (Revelio seniority >=
   --senior-min, default 6 = executive and senior executive) and, unless --all-us, all U.S. employees.
   --all-us pulls executive positions only, since all positions of every U.S. borrower would be too large.
4. Co-ethnic measures for each loan in data/bank_dealscan_loans.csv: the borrower's share of the lending bank's
   FDIC group (Asian American bank -> api share, ...) among executives and all employees, at the December 31
   before the loan (tranche active date, else deal active date). Missing when Revelio has no snapshot.

Outputs: data/dealscan_borrower_revelio_match[_all_us].csv
         data/dealscan_borrower_ethnicity_year[_all_us].csv   borrower x year
         data/bank_dealscan_loans_coethnic.csv                 the banks' loans with co-ethnic measures
         the same tables in data/ethnic_banks.sqlite
Run where WRDS is reachable (needs pandas, pyarrow, wrds), after link_dealscan.py:
  python link_borrowers.py --match-only          # review data/dealscan_borrower_revelio_match.csv
  python link_borrowers.py --use-existing-match  # pull workforce, build shares and co-ethnic measures
DealScan- and Revelio-derived outputs are licensed data: they are git-ignored, keep them out of the public repo.
"""
import argparse
import os
import sqlite3
import sys
from pathlib import Path

import pandas as pd

from build_db import GROUPS
from link_dealscan import LIB, TABLE, id_str, resolve_columns, select_list
from link_revelio import (BATCH_RCID, DATA, ETH, PARTS, RV_COLS, US, annual_shares, cached, match, pull_workforce,
                          snapshot_years, sql_list)

SENIOR_MIN = 6
RCID_STEP = 2_000_000
# FDIC group -> Revelio ethnicity, in build_db.GROUPS order
GROUP_ETH = dict(zip(GROUPS, ["black", "hispanic", "api", "native", "multiple"]))
BORROWER_COLS = ["borrower_id", "borrower_name", "borrower_city", "borrower_state", "borrower_country"]


def borrower_entities(b: pd.DataFrame, bid: str) -> pd.DataFrame:
    """One row per borrower with the columns link_revelio.match expects."""
    out = pd.DataFrame({bid: b[bid].map(id_str), "name": b["borrower_name"],
                        "city": b.get("borrower_city"), "state": b.get("borrower_state")})
    return out.dropna(subset=[bid]).drop_duplicates(bid).reset_index(drop=True)


def borrower_year(pos, eth, years, senior_min, all_employees=True) -> pd.DataFrame:
    """rcid x year: exec_* shares (seniority >= senior_min) and, if all_employees, all-employee shares."""
    parts = []
    if all_employees:
        parts.append(annual_shares(pos, eth, years))
    if "seniority" in pos:
        ex = annual_shares(pos[pos.seniority >= senior_min], eth, years)
        parts.append(ex.rename(columns={c: f"exec_{c}" for c in ex.columns if c not in ("rcid", "year")}))
    out = parts[0]
    for p in parts[1:]:
        out = out.merge(p, on=["rcid", "year"], how="outer")
    return out


def group_share(d: pd.DataFrame, prefix: str, eth_key: pd.Series) -> pd.Series:
    """Row-wise d[prefix + eth_key], NaN where the key or the column is missing."""
    out = pd.Series(float("nan"), index=d.index)
    for e in ETH:
        if prefix + e in d:
            out = out.mask(eth_key == e, d[prefix + e])
    return out


def coethnic(loans: pd.DataFrame, banks: pd.DataFrame, by: pd.DataFrame, bid: str) -> pd.DataFrame:
    """The banks' loans with the borrower's share of the bank's group at the December 31 before the loan."""
    d = loans.merge(banks[["cert", "ethnic_group"]], on="cert", how="left")
    date = d["tranche_date"] if "tranche_date" in d else d["deal_date"]
    d["snapshot_year"] = (pd.to_datetime(date, errors="coerce").dt.year - 1).astype("Int64")
    d[bid] = d[bid].map(id_str)
    by = by.rename(columns={"year": "snapshot_year"}).astype({"snapshot_year": "Int64"})
    d = d.merge(by, on=[bid, "snapshot_year"], how="left")
    key = d.ethnic_group.map(GROUP_ETH)
    d["coethnic_share"] = group_share(d, "share_", key)
    d["coethnic_exec_share"] = group_share(d, "exec_share_", key)
    return d


def pull_us_companies(db) -> pd.DataFrame:
    mx = int(db.raw_sql("select max(rcid) as m from revelio_common.company_mapping")["m"].iloc[0])
    parts = []
    for lo in range(0, mx + 1, RCID_STEP):
        parts.append(cached(PARTS / f"rv_us_{lo // RCID_STEP:04d}.parquet", lambda: db.raw_sql(
            f"select {RV_COLS} from revelio_common.company_mapping where hq_country in ({sql_list(US)}) "
            f"and rcid >= {lo} and rcid < {lo + RCID_STEP}")))
        print(f"U.S. companies, rcid < {lo + RCID_STEP:,}: {sum(len(p) for p in parts):,}", flush=True)
    return pd.concat(parts, ignore_index=True)


def load_company_mapping(folder: Path) -> pd.DataFrame:
    cols = [c.strip() for c in RV_COLS.split(",")]
    rv = pd.concat([pd.read_parquet(p, columns=cols) for p in sorted(folder.glob("*.parquet"))], ignore_index=True)
    return rv[rv.hq_country.isin(US)]


def main(argv=None):
    ap = argparse.ArgumentParser(description="Borrower ethnicity from Revelio and co-ethnic lending measures.")
    ap.add_argument("--banks", type=Path, default=DATA / "ethnic_banks.csv")
    ap.add_argument("--loans", type=Path, default=DATA / "bank_dealscan_loans.csv")
    ap.add_argument("--all-us", action="store_true", help="every U.S. DealScan borrower, executives only")
    ap.add_argument("--company-mapping", type=Path, help="folder of local company_mapping parquet files")
    ap.add_argument("--senior-min", type=int, default=SENIOR_MIN, help="lowest Revelio seniority counted as executive")
    ap.add_argument("--match-only", action="store_true", help="stop after writing the match file for review")
    ap.add_argument("--use-existing-match", action="store_true", help="skip matching and use the match file as edited")
    args = ap.parse_args(argv)

    import wrds
    sys.stdout.reconfigure(encoding="utf-8")
    PARTS.mkdir(parents=True, exist_ok=True)
    banks = pd.read_csv(args.banks, dtype={"zip": str})
    ids = {c: str for c in ("lender_id", "borrower_id", "deal_id", "tranche_id")}
    loans = pd.read_csv(args.loans, dtype=ids) if args.loans.exists() else None
    if loans is None and not args.all_us:
        raise SystemExit(f"{args.loans} not found; run link_dealscan.py first.")
    tag = "_all_us" if args.all_us else ""
    match_path = DATA / f"dealscan_borrower_revelio_match{tag}.csv"

    db = wrds.Connection(wrds_username=os.environ.get("WRDS_USERNAME"))
    try:
        if args.all_us:
            cols = resolve_columns(list(db.describe_table(library=LIB, table=TABLE)["name"]))
            bid = "borrower_id" if "borrower_id" in cols else "borrower_name"
        else:
            bid = "borrower_id" if "borrower_id" in loans else "borrower_name"
        if args.use_existing_match:
            m = pd.read_csv(match_path, dtype={bid: str, "rcid": "Int64"})
        else:
            if args.all_us:
                where = f" where {cols['borrower_country']} in ({sql_list(US)})" if "borrower_country" in cols else ""
                q = f"select distinct {select_list(cols, BORROWER_COLS)} from {LIB}.{TABLE}{where}"
                borrowers = cached(PARTS / "dealscan_us_borrowers.parquet", lambda: db.raw_sql(q))
            else:
                borrowers = loans
            if "borrower_name" not in borrowers:
                raise SystemExit("DealScan data has no borrower name column; cannot match borrowers to Revelio.")
            ent = borrower_entities(borrowers, bid)
            rv = load_company_mapping(args.company_mapping) if args.company_mapping else pull_us_companies(db)
            print(f"borrowers: {len(ent):,}; U.S. Revelio companies: {len(rv):,}")
            m = match(ent, rv, id_col=bid, banking=False)
            m.to_csv(match_path, index=False)
            print(f"matched {m.rcid.notna().sum():,} of {len(m):,} borrowers -> {match_path}")
            print(m.match_tier.value_counts().to_string())
            if args.match_only:
                return

        rcids = sorted(m.rcid.dropna().astype("int64").unique().tolist())
        if not rcids:
            raise SystemExit(f"No borrower is matched to a Revelio rcid in {match_path}.")
        pos_cols = set(db.describe_table(library="revelio", table="individual_positions")["name"])
        if "seniority" in pos_cols:
            q = ",".join(map(str, rcids[:BATCH_RCID]))
            lo, hi = db.raw_sql(f"select min(seniority) as lo, max(seniority) as hi from revelio.individual_positions "
                                f"where rcid in ({q})").iloc[0]
            print(f"Revelio seniority runs {lo}-{hi}; executives are >= {args.senior_min}")
            if pd.notna(hi) and hi < args.senior_min:
                raise SystemExit(f"--senior-min {args.senior_min} is above the seniority scale; pick a lower level.")
        elif not args.all_us:
            print("warning: Revelio positions have no seniority; only all-employee shares are built")
        pos, eth, _ = pull_workforce(db, rcids, senior_min=args.senior_min if args.all_us else None)
    finally:
        db.close()

    years = snapshot_years(pos)
    link = m.dropna(subset=["rcid"])[[bid, "rcid"]].astype({"rcid": "int64"})
    link[bid] = link[bid].map(id_str)
    by = link.merge(borrower_year(pos, eth, years, args.senior_min, all_employees=not args.all_us), on="rcid")
    by.to_csv(DATA / f"dealscan_borrower_ethnicity_year{tag}.csv", index=False)
    print(f"borrower-years: {len(by):,} for {by[bid].nunique():,} borrowers, {years.start}-{years.stop - 1}")
    tables = {f"dealscan_borrower_revelio_match{tag}": m, f"dealscan_borrower_ethnicity_year{tag}": by}

    if loans is not None:
        co = coethnic(loans, banks, by, bid)
        co.to_csv(DATA / "bank_dealscan_loans_coethnic.csv", index=False)
        tables["dealscan_loans_coethnic"] = co
        measure = [c for c in ("coethnic_exec_share", "coethnic_share") if co[c].notna().any()]
        if measure:
            print(f"\nLoans with a co-ethnic measure: {co[measure[0]].notna().sum():,} of {len(co):,}")
            print("Mean co-ethnic share of the banks' borrowers, by bank group:")
            print(co.groupby("ethnic_group")[measure].mean().round(3).to_string())
        if args.all_us and "exec_share_api" in by:
            base = by[[f"exec_share_{e}" for e in ETH]].mean().round(3)
            print("Baseline, mean executive shares across all U.S. DealScan borrower-years:")
            print(base.to_string())

    db_path = DATA / "ethnic_banks.sqlite"
    if db_path.exists():
        with sqlite3.connect(db_path) as con:
            for name, df in tables.items():
                df.to_sql(name, con, if_exists="replace", index=False)
        print(f"added {', '.join(tables)} to {db_path}")


if __name__ == "__main__":
    main()
