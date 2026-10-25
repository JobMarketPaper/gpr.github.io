"""
link_dealscan.py
Link the ethnic-bank database to LSEG DealScan (WRDS: dealscan.dealscan, one row per tranche x lender) and
build the banks' syndicated-loan participations and a bank-year lending summary.

1. Lenders: distinct DealScan lenders (id, name, parent, country), kept to U.S. lenders when DealScan reports
   a lender country. Matched to FDIC banks, in order:
     D1  standardized lender name == standardized bank name, one bank per name (a bank may have several
         DealScan lender ids)
     D2  standardized lender parent == standardized bank holding company, unique among the banks, for
         lenders with a bank-like name
   `accept` defaults to True unless a D1 name is shared by lenders with different parents in DealScan and
   the parent does not agree with the bank or its holding company. Review and flip `accept` by hand.
2. Participations: every DealScan row for the accepted lender ids.
3. Bank-year summary by tranche active year (deal active year if DealScan has no tranche date): tranches,
   deals, tranches as lead arranger, and total tranche amount (as reported by DealScan, one count per tranche).

DealScan column names are resolved at run time from describe_table; the script prints what it used and stops
with the full column list if a required one is missing.

Inputs : data/ethnic_banks.csv (from build_db.py)
Outputs: data/bank_dealscan_lender_match.csv   one row per matched DealScan lender id
         data/bank_dealscan_loans.csv          one row per tranche x lender for accepted lenders
         data/bank_dealscan_year.csv           cert x year
         the same tables in data/ethnic_banks.sqlite
Run where WRDS is reachable (needs pandas, pyarrow, wrds). Set WRDS_USERNAME or let wrds prompt.
  python link_dealscan.py --match-only          # review data/bank_dealscan_lender_match.csv
  python link_dealscan.py --use-existing-match  # pull participations for rows with accept = True
DealScan-derived outputs are licensed data: they are git-ignored, keep them out of the public repo.
"""
import argparse
import os
import sqlite3
import sys
from pathlib import Path

import pandas as pd

from link_revelio import BANKLIKE_RE, DATA, PARTS, US, cached, digest, norm_name, pull_batches

LIB, TABLE = "dealscan", "dealscan"
BATCH_LENDER = 500
# standard name -> DealScan column candidates, first present wins
COLUMNS = {
    "lender_id": ["lender_id", "lenderid", "lender_company_id"],
    "lender_name": ["lender_name", "lender"],
    "lender_parent_id": ["lender_parent_id", "lender_ultimate_parent_id"],
    "lender_parent_name": ["lender_parent_name", "lender_ultimate_parent_name", "lender_parent"],
    "lender_country": ["lender_operating_country", "lender_country", "lender_parent_operating_country"],
    "lender_role": ["lender_role", "role"],
    "lead_arranger": ["lead_arranger", "lead_arranger_credit"],
    "lender_share": ["lender_share", "bank_allocation", "lender_allocation"],
    "deal_id": ["lpc_deal_id", "deal_id"],
    "tranche_id": ["lpc_tranche_id", "tranche_id", "facility_id"],
    "deal_date": ["deal_active_date"],
    "tranche_date": ["tranche_active_date"],
    "tranche_amount": ["tranche_amount_converted", "tranche_amount_usd", "tranche_amount"],
    "deal_amount": ["deal_amount_converted", "deal_amount_usd", "deal_amount"],
    "currency": ["tranche_currency", "currency", "deal_currency"],
    "tranche_type": ["tranche_type"],
    "market_segment": ["market_segment"],
    "primary_purpose": ["primary_purpose", "deal_purpose"],
    "spread_bps": ["all_in_spread_drawn_bps", "margin_bps"],
    "maturity_date": ["tranche_maturity_date", "maturity_date"],
    "borrower_id": ["borrower_id"],
    "borrower_name": ["borrower_name"],
    "borrower_city": ["city", "borrower_city"],
    "borrower_state": ["state_province", "borrower_state", "state"],
    "borrower_country": ["country", "borrower_country"],
    "borrower_sic": ["sic_code", "primary_sic_code", "borrower_sic"],
}
LENDER_COLS = ["lender_id", "lender_name", "lender_parent_name", "lender_country"]


def resolve_columns(available):
    """{standard name: DealScan column} for the columns present; exits if a required one is missing."""
    lower = {c.lower(): c for c in available}
    cols = {}
    for std, cands in COLUMNS.items():
        hit = next((lower[c] for c in cands if c in lower), None)
        if hit:
            cols[std] = hit
    missing = [k for k in ("lender_name",) if k not in cols]
    if not ({"tranche_id", "deal_id"} & cols.keys()):
        missing.append("tranche_id or deal_id")
    if not ({"tranche_date", "deal_date"} & cols.keys()):
        missing.append("tranche_date or deal_date")
    if missing:
        raise SystemExit(f"DealScan is missing {missing}. Columns: {sorted(available)}")
    cols.setdefault("lender_id", cols["lender_name"])  # fall back to the name as the lender key
    return cols


def select_list(cols, names):
    return ", ".join(f"{cols[n]} as {n}" if n in cols else f"null as {n}" for n in names)


def id_str(x):
    if pd.isna(x):
        return None
    return str(int(x)) if isinstance(x, float) and x.is_integer() else str(x)


# ---------- matching ----------
def match_lenders(banks: pd.DataFrame, lenders: pd.DataFrame) -> pd.DataFrame:
    L = lenders.copy()
    L["lender_id"] = L.lender_id.map(id_str)
    if L.lender_country.notna().any():
        L = L[L.lender_country.isna() | L.lender_country.isin(US)]
    L["name_n"], L["parent_n"] = norm_name(L.lender_name), norm_name(L.lender_parent_name)
    L = L.dropna(subset=["lender_id", "name_n"])
    # several lender ids under one name, not tied together by a single parent: may be different banks
    by_name = L.groupby("name_n")
    L["name_shared"] = (by_name["lender_id"].transform("nunique") > 1) & (by_name["parent_n"].transform("nunique") != 1)

    B = banks[["cert", "name", "holding_company"]].copy()
    B["bank_n"], B["hc_n"] = norm_name(B.name), norm_name(B.holding_company)

    b1 = B.dropna(subset=["bank_n"])
    b1 = b1[b1.groupby("bank_n")["cert"].transform("size") == 1]
    d1 = L.merge(b1[["cert", "bank_n", "hc_n"]], left_on="name_n", right_on="bank_n").assign(match_tier="D1_name")

    b2 = B.dropna(subset=["hc_n"])
    b2 = b2[b2.groupby("hc_n")["cert"].transform("size") == 1]
    rest = L[~L.lender_id.isin(d1.lender_id) & L.name_n.str.contains(BANKLIKE_RE)]
    d2 = rest.merge(b2[["cert", "bank_n", "hc_n"]], left_on="parent_n", right_on="hc_n").assign(match_tier="D2_parent")

    m = pd.concat([d1, d2], ignore_index=True)
    m["parent_agrees"] = m.parent_n.notna() & ((m.parent_n == m.hc_n) | (m.parent_n == m.bank_n))
    m["accept"] = m.parent_agrees | ~m.name_shared
    m = m.drop_duplicates("lender_id")
    m = m.merge(banks[["cert", "name"] + [c for c in ("ethnic_group", "state") if c in banks]], on="cert")
    m = m.rename(columns={"name": "bank_name"})
    return m[["cert", "bank_name", *[c for c in ("ethnic_group", "state") if c in m], "lender_id", "lender_name",
              "lender_parent_name", "lender_country", "match_tier", "name_shared", "parent_agrees", "accept"]]


# ---------- summaries ----------
def lead_flag(d: pd.DataFrame) -> pd.Series:
    if "lead_arranger" in d:
        return d.lead_arranger.astype("string").str.strip().str.lower().isin(["yes", "y", "1", "true"])
    if "lender_role" in d:
        return d.lender_role.astype("string").str.contains(r"arrang|agent|lead", case=False, na=False)
    return pd.Series(False, index=d.index)


def bank_year(loans: pd.DataFrame) -> pd.DataFrame:
    date = loans["tranche_date"] if "tranche_date" in loans else loans["deal_date"]
    d = loans.assign(year=pd.to_datetime(date, errors="coerce").dt.year).dropna(subset=["year"])
    d["year"] = d.year.astype(int)
    key = "tranche_id" if "tranche_id" in d else "deal_id"
    g = d.groupby(["cert", "year"])
    out = g[key].nunique().rename("n_tranches").to_frame()
    if "deal_id" in d:
        out["n_deals"] = g["deal_id"].nunique()
    out["n_tranches_lead"] = d[lead_flag(d)].groupby(["cert", "year"])[key].nunique()
    out["n_tranches_lead"] = out.n_tranches_lead.fillna(0).astype(int)
    if "tranche_amount" in d:
        per_tranche = d.drop_duplicates(["cert", key]).assign(tranche_amount=lambda x: pd.to_numeric(x.tranche_amount))
        out["tranche_amount_sum"] = per_tranche.groupby(["cert", "year"])["tranche_amount"].sum(min_count=1)
    return out.reset_index()


def main(argv=None):
    ap = argparse.ArgumentParser(description="Link ethnic banks to DealScan lenders and their syndicated loans.")
    ap.add_argument("--banks", type=Path, default=DATA / "ethnic_banks.csv")
    ap.add_argument("--match-only", action="store_true", help="stop after writing the lender match for review")
    ap.add_argument("--use-existing-match", action="store_true",
                    help="skip matching and use data/bank_dealscan_lender_match.csv as edited")
    args = ap.parse_args(argv)

    import wrds
    sys.stdout.reconfigure(encoding="utf-8")
    PARTS.mkdir(parents=True, exist_ok=True)
    banks = pd.read_csv(args.banks, dtype={"zip": str})
    match_path = DATA / "bank_dealscan_lender_match.csv"
    db = wrds.Connection(wrds_username=os.environ.get("WRDS_USERNAME"))
    try:
        cols = resolve_columns(list(db.describe_table(library=LIB, table=TABLE)["name"]))
        print("DealScan columns used:", cols)
        if args.use_existing_match:
            lm = pd.read_csv(match_path, dtype={"lender_id": str})
        else:
            q = f"select distinct {select_list(cols, LENDER_COLS)} from {LIB}.{TABLE}"
            lenders = cached(PARTS / f"dealscan_lenders_{digest(sorted(cols.items()))}.parquet", lambda: db.raw_sql(q))
            print(f"DealScan lenders: {len(lenders):,}")
            lm = match_lenders(banks, lenders)
            lm.to_csv(match_path, index=False)
            print(f"matched {lm.cert.nunique()} of {len(banks)} banks via {len(lm)} lender ids -> {match_path}")
            print(lm.groupby(["match_tier", "accept"]).size().to_string())
            if args.match_only:
                return

        lm["accept"] = lm.accept.astype(str).str.lower().isin(["true", "1", "yes"])
        ids = sorted(lm.loc[lm.accept, "lender_id"].dropna().map(id_str).unique().tolist())
        if not ids:
            raise SystemExit(f"No accepted DealScan lender in {match_path}.")
        names = [n for n in COLUMNS if n in cols]
        loans = pull_batches(db, ids, BATCH_LENDER, "dealscan_loans", lambda q: f"""
            select {select_list(cols, names)} from {LIB}.{TABLE} where {cols['lender_id']} in ({q})""")
    finally:
        db.close()

    loans["lender_id"] = loans.lender_id.map(id_str)
    loans = lm.loc[lm.accept, ["cert", "lender_id"]].merge(loans, on="lender_id")
    year = bank_year(loans)
    loans.to_csv(DATA / "bank_dealscan_loans.csv", index=False)
    year.to_csv(DATA / "bank_dealscan_year.csv", index=False)
    print(f"participations: {len(loans):,} rows for {loans.cert.nunique()} banks; bank-years: {len(year):,}")

    db_path = DATA / "ethnic_banks.sqlite"
    if db_path.exists():
        with sqlite3.connect(db_path) as con:
            for name, df in {"dealscan_lender_match": lm, "dealscan_loans": loans, "dealscan_year": year}.items():
                df.to_sql(name, con, if_exists="replace", index=False)
        print(f"added dealscan_lender_match, dealscan_loans, dealscan_year to {db_path}")


if __name__ == "__main__":
    main()
