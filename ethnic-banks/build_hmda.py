"""
build_hmda.py
Mortgage lending of the ethnic banks from HMDA (2018 on), with each applicant's race and ethnicity, including
the detailed Asian, Pacific Islander and Hispanic subgroups HMDA has collected since 2018.

1. Lenders: each bank's HMDA LEI. With --panel (HMDA Reporter Panel CSVs, 2018-2023, from the FFIEC data
   publication pages), by respondent_rssd = FDIC fed_rssd. Banks the panel misses are matched by standardized
   name against the Data Browser filer list; a name used by several LEIs is left with accept = False.
   Review data/bank_hmda_lei_match.csv and flip `accept` as needed.
2. Applications: the Data Browser loan-level CSV for the accepted LEIs, each year (cached in data/_hmda_raw).
3. Applicant group from the applicant's own codes (co-applicant ignored), consistent with build_sod.py:
   hispanic if any Hispanic ethnicity code; otherwise the minority race reported (api = Asian or Pacific
   Islander, native = American Indian or Alaska Native), multiple if two or more minority races, white if
   only white, missing otherwise. applicant_subgroup is the first detailed code (Chinese, Korean, Mexican, ...).
   coethnic = 1 if the applicant's group is the bank's FDIC group.
4. Bank-year summary: applications (actions 1-5), originations, denial rate (denied / actions 1-3),
   origination shares by group, co-ethnic share of originations, and denial rates for co-ethnic and other
   applicants; plus originations by subgroup.
5. Baseline (--baseline-counties N): all lenders' applications in the N counties where the ethnic banks take
   the most applications. County-year group shares of all originations give the co-ethnic share expected from
   local demand (expected_coethnic_share_bc), next to the banks' own share in those counties (coethnic_share_bc).

Outputs (public data): data/bank_hmda_lei_match.csv, data/bank_hmda_applications.parquet,
    data/bank_hmda_year.csv, data/bank_hmda_subgroup_year.csv, data/hmda_county_year_baseline.csv,
    and the summary tables in data/ethnic_banks.sqlite
Usage: python3 build_hmda.py --match-only [--panel panel_2022.csv ...]    then review the match file
       python3 build_hmda.py --use-existing-match [--baseline-counties 10]
Needs pandas and pyarrow.
"""
import argparse
import hashlib
import shutil
import sqlite3
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from contextlib import closing
from datetime import date
from pathlib import Path

import pandas as pd

from build_db import GROUP_ETH, OUT_DIR, get_json
from link_revelio import norm_name

DATA = OUT_DIR
RAW = DATA / "_hmda_raw"
API = "https://ffiec.cfpb.gov/v2/data-browser-api/view"
FIRST_YEAR = 2018
ETH = ["white", "black", "api", "hispanic", "native", "multiple"]
RACE_COLS = [f"applicant_race-{i}" for i in range(1, 6)]
ETHN_COLS = [f"applicant_ethnicity-{i}" for i in range(1, 6)]
HISPANIC_CODES = {1, 11, 12, 13, 14}
# minority race codes -> bit (api 1, black 2, native 4); white is 5
RACE_BIT = {1: 4, 2: 1, 3: 2, 4: 1, **{c: 1 for c in range(21, 28)}, **{c: 1 for c in range(41, 45)}}
BIT_GROUP = {1: "api", 2: "black", 4: "native"}
SUBGROUP = {21: "Asian Indian", 22: "Chinese", 23: "Filipino", 24: "Japanese", 25: "Korean", 26: "Vietnamese",
            27: "Other Asian", 41: "Native Hawaiian", 42: "Guamanian or Chamorro", 43: "Samoan",
            44: "Other Pacific Islander", 11: "Mexican", 12: "Puerto Rican", 13: "Cuban", 14: "Other Hispanic or Latino"}
KEEP = ["activity_year", "lei", "state_code", "county_code", "census_tract", "action_taken", "loan_type", "loan_purpose",
        "lien_status", "occupancy_type", "loan_amount", "interest_rate", "rate_spread", "income", "property_value",
        "debt_to_income_ratio", "derived_ethnicity", "derived_race", "tract_minority_population_percent",
        *ETHN_COLS, *RACE_COLS]


# ---------- applicant classification ----------
def applicant_group(df: pd.DataFrame) -> pd.Series:
    races = df[RACE_COLS].apply(pd.to_numeric, errors="coerce")
    ethn = df[ETHN_COLS].apply(pd.to_numeric, errors="coerce")
    bits = pd.Series(0, index=df.index)
    for c in RACE_COLS:
        bits = bits | races[c].map(RACE_BIT).fillna(0).astype(int)
    group = pd.Series(pd.NA, index=df.index, dtype="string")
    group = group.mask((races == 5).any(axis=1) & (bits == 0), "white")
    group = group.mask(bits.isin(list(BIT_GROUP)), bits.map(BIT_GROUP))
    group = group.mask(bits.isin([3, 5, 6, 7]), "multiple")  # two or more minority races
    return group.mask(ethn.isin(HISPANIC_CODES).any(axis=1), "hispanic")


def applicant_subgroup(df: pd.DataFrame, group: pd.Series) -> pd.Series:
    def first_detail(cols, codes):
        out = pd.Series(pd.NA, index=df.index, dtype="string")
        for c in reversed(cols):  # earlier columns win
            v = pd.to_numeric(df[c], errors="coerce")
            out = out.mask(v.isin(codes), v.map(SUBGROUP))
        return out
    race_sub = first_detail(RACE_COLS, [c for c in SUBGROUP if c > 20])
    hisp_sub = first_detail(ETHN_COLS, [11, 12, 13, 14])
    return race_sub.where((group == "api").fillna(False)).mask((group == "hispanic").fillna(False), hisp_sub)


def classify(apps: pd.DataFrame, bank_eth: pd.Series) -> pd.DataFrame:
    apps = apps.copy()
    apps["applicant_group"] = applicant_group(apps)
    apps["applicant_subgroup"] = applicant_subgroup(apps, apps.applicant_group)
    apps["coethnic"] = (apps.applicant_group == bank_eth).astype(float).where(apps.applicant_group.notna())
    return apps


# ---------- lenders ----------
def match_leis(banks: pd.DataFrame, filers: pd.DataFrame, panel=None) -> pd.DataFrame:
    """cert <-> LEI candidates: panel RSSD matches first, then unique standardized names."""
    found = []
    if panel is not None and len(panel):
        p = panel[["lei", "respondent_rssd"]].dropna().drop_duplicates()
        p = p.astype({"respondent_rssd": "int64"})
        b = banks[["cert", "fed_rssd"]].dropna().astype("int64")
        found.append(b.merge(p, left_on="fed_rssd", right_on="respondent_rssd")[["cert", "lei"]]
                     .assign(method="panel_rssd", accept=True))
    done_certs = set(found[0].cert) if found else set()
    done_leis = set(found[0].lei) if found else set()

    f = filers.dropna(subset=["lei"]).drop_duplicates("lei", keep="last").copy()
    f["name_n"] = norm_name(f["name"])
    f = f.dropna(subset=["name_n"])
    f["n_leis"] = f.groupby("name_n")["lei"].transform("size")
    b = banks[["cert", "name"]].copy()
    b["name_n"] = norm_name(b["name"])
    b = b.dropna(subset=["name_n"])
    b = b[b.groupby("name_n")["cert"].transform("size") == 1]
    n = b[~b.cert.isin(done_certs)].merge(f[~f.lei.isin(done_leis)][["lei", "name_n", "n_leis"]], on="name_n")
    found.append(n.assign(method="name", accept=n.n_leis == 1)[["cert", "lei", "method", "accept"]])

    m = pd.concat(found, ignore_index=True).drop_duplicates(["cert", "lei"])
    names = filers.dropna(subset=["lei"]).drop_duplicates("lei", keep="last").set_index("lei")["name"]
    m = m.merge(banks[["cert", "name"] + [c for c in ("ethnic_group", "state") if c in banks]], on="cert")
    m["hmda_name"] = m.lei.map(names)
    return m.rename(columns={"name": "bank_name"})


# ---------- summaries ----------
def bank_year(apps: pd.DataFrame) -> pd.DataFrame:
    keys = ["cert", "activity_year"]
    a = apps[apps.action_taken.between(1, 5)]
    g = a.groupby(keys)
    out = g.size().rename("n_applications").to_frame()
    orig = a[a.action_taken == 1]
    out["n_originations"] = orig.groupby(keys).size()
    dec = a[a.action_taken.isin([1, 2, 3])].assign(denied=lambda x: (x.action_taken == 3).astype(float))
    out["denial_rate"] = dec.groupby(keys).denied.mean()
    known = orig[orig.applicant_group.notna()]
    shares = pd.crosstab([known.cert, known.activity_year], known.applicant_group, normalize="index")
    shares = shares.reindex(columns=ETH, fill_value=0.0).add_prefix("orig_share_")
    shares.index.names = keys
    out = out.join(shares)
    out["coethnic_orig_share"] = known.groupby(keys).coethnic.mean()
    dk = dec[dec.coethnic.notna()]
    out["denial_rate_coethnic"] = dk[dk.coethnic == 1].groupby(keys).denied.mean()
    out["denial_rate_other"] = dk[dk.coethnic == 0].groupby(keys).denied.mean()
    out[["n_originations"]] = out[["n_originations"]].fillna(0).astype(int)
    return out.reset_index()


def subgroup_year(apps: pd.DataFrame) -> pd.DataFrame:
    o = apps[(apps.action_taken == 1) & apps.applicant_subgroup.notna()]
    return o.groupby(["cert", "activity_year", "applicant_group", "applicant_subgroup"]).size() \
        .rename("n_originations").reset_index()


def county_baseline(lar: pd.DataFrame) -> pd.DataFrame:
    """activity_year x county_code: each group's share of all lenders' originations (known group)."""
    o = lar[lar.action_taken == 1]
    grp = applicant_group(o)
    o = o.assign(applicant_group=grp)[grp.notna()]
    s = pd.crosstab([o.activity_year, o.county_code], o.applicant_group, normalize="index")
    s = s.reindex(columns=ETH, fill_value=0.0).add_prefix("county_share_")
    s["n_originations_all_lenders"] = o.groupby(["activity_year", "county_code"]).size()
    s.index.names = ["activity_year", "county_code"]
    return s.reset_index()


def baseline_compare(apps: pd.DataFrame, base: pd.DataFrame, bank_eth: pd.Series) -> pd.DataFrame:
    """Per cert-year, in baseline counties: the banks' co-ethnic origination share and the share expected
    from all lenders' originations in the same county-years."""
    o = apps[(apps.action_taken == 1) & apps.coethnic.notna()].copy()
    o["bank_eth"] = bank_eth.reindex(o.index)
    o = o.merge(base, on=["activity_year", "county_code"], how="inner")
    o["expected"] = pd.Series(float("nan"), index=o.index)
    for e in ETH:
        o["expected"] = o.expected.mask(o.bank_eth == e, o[f"county_share_{e}"])
    g = o.groupby(["cert", "activity_year"])
    return pd.DataFrame({"coethnic_share_bc": g.coethnic.mean(), "expected_coethnic_share_bc": g.expected.mean(),
                         "n_originations_bc": g.size()}).reset_index()


# ---------- HMDA API ----------
def download(url: str, path: Path, attempts=3):
    if path.exists():
        return path
    tmp = path.with_suffix(".part")
    for i in range(attempts):
        try:
            with urllib.request.urlopen(url, timeout=600) as resp, open(tmp, "wb") as f:
                shutil.copyfileobj(resp, f, length=1 << 20)
            tmp.rename(path)
            return path
        except urllib.error.HTTPError as e:
            if e.code < 500 or i == attempts - 1:
                raise
        except urllib.error.URLError:
            if i == attempts - 1:
                raise
        time.sleep(2 ** (i + 1))


def read_lar(path: Path) -> pd.DataFrame:
    df = pd.read_csv(path, usecols=lambda c: c in KEEP, dtype=str, low_memory=False)
    df["activity_year"] = pd.to_numeric(df.activity_year, errors="coerce").astype("Int64")
    for c in ("action_taken", "loan_amount", "interest_rate", "rate_spread", "income", "property_value",
              "tract_minority_population_percent"):
        if c in df:
            df[c] = pd.to_numeric(df[c], errors="coerce")
    return df


def filers():
    """(HMDA filers with lei, name, year; the years the Data Browser serves)."""
    rows, years = [], []
    for y in range(FIRST_YEAR, date.today().year):
        try:
            inst = get_json(f"{API}/filers?years={y}")["institutions"]
        except (urllib.error.URLError, ValueError, KeyError):
            continue
        years.append(y)
        rows += [{**r, "year": y} for r in inst]
    if not years:
        raise SystemExit("Could not reach the HMDA Data Browser API (ffiec.cfpb.gov).")
    return pd.DataFrame(rows).sort_values("year"), years


def load_panel(paths):
    frames = []
    for p in paths:
        df = pd.read_csv(p, dtype=str, sep=None, engine="python")
        df.columns = [c.strip().lower() for c in df.columns]
        if not {"lei", "respondent_rssd"} <= set(df.columns):
            raise SystemExit(f"{p} has no lei / respondent_rssd columns: {list(df.columns)[:15]}")
        df["respondent_rssd"] = pd.to_numeric(df.respondent_rssd, errors="coerce")
        frames.append(df[df.respondent_rssd > 0][["lei", "respondent_rssd"]])
    return pd.concat(frames, ignore_index=True) if frames else None


def digest(values):
    return hashlib.md5(",".join(map(str, values)).encode()).hexdigest()[:10]


def pull_lar(years, param, values, tag):
    """Loan-level CSVs for `values` of `param` (leis or counties), one file per year and batch of 100."""
    frames = []
    for y in years:
        for i in range(0, len(values), 100):
            batch = values[i:i + 100]
            path = RAW / f"{tag}_{y}_{digest(batch)}.csv"
            download(f"{API}/csv?" + urllib.parse.urlencode({"years": y, param: ",".join(batch)}, safe=","), path)
            frames.append(read_lar(path))
            print(f"HMDA {tag} {y} batch {i // 100 + 1}: {len(frames[-1]):,} rows", flush=True)
    return pd.concat(frames, ignore_index=True)


def main(argv=None):
    ap = argparse.ArgumentParser(description="Ethnic banks' HMDA mortgage lending with applicant race and ethnicity.")
    ap.add_argument("--banks", type=Path, default=DATA / "ethnic_banks.csv")
    ap.add_argument("--panel", type=Path, nargs="+", default=[], help="HMDA Reporter Panel files (lei, respondent_rssd)")
    ap.add_argument("--match-only", action="store_true", help="stop after writing the LEI match for review")
    ap.add_argument("--use-existing-match", action="store_true", help="use data/bank_hmda_lei_match.csv as edited")
    ap.add_argument("--baseline-counties", type=int, default=0, help="all lenders in the top N counties (0 = off)")
    args = ap.parse_args(argv)

    RAW.mkdir(parents=True, exist_ok=True)
    banks = pd.read_csv(args.banks, dtype={"zip": str})
    match_path = DATA / "bank_hmda_lei_match.csv"
    filer_list, years = filers()
    print(f"HMDA years available: {years[0]}-{years[-1]}")
    if args.use_existing_match:
        lm = pd.read_csv(match_path, dtype={"lei": str})
        lm["accept"] = lm.accept.astype(str).str.lower().isin(["true", "1", "yes"])
    else:
        lm = match_leis(banks, filer_list, load_panel(args.panel))
        lm.to_csv(match_path, index=False)
        print(f"matched {lm[lm.accept].cert.nunique()} of {len(banks)} banks to HMDA LEIs -> {match_path}")
        print(lm.groupby(["method", "accept"]).size().to_string())
        if args.match_only:
            return

    link = lm[lm.accept].drop_duplicates("lei")[["lei", "cert"]]
    if link.empty:
        sys.exit(f"No accepted LEI in {match_path}.")
    apps = pull_lar(years, "leis", sorted(link.lei), "banks").merge(link, on="lei")
    bank_eth = apps.cert.map(banks.set_index("cert").ethnic_group).map(GROUP_ETH)
    apps = classify(apps, bank_eth)
    apps.to_parquet(DATA / "bank_hmda_applications.parquet", index=False)

    year = bank_year(apps)
    sub = subgroup_year(apps)
    tables = {"bank_hmda_lei_match": lm, "bank_hmda_subgroup_year": sub}
    if args.baseline_counties > 0:
        top = apps[apps.action_taken.between(1, 5)].county_code.dropna()
        counties = top[top.str.fullmatch(r"\d{5}")].value_counts().head(args.baseline_counties).index.tolist()
        print(f"baseline counties: {', '.join(counties)}")
        base = county_baseline(pull_lar(years, "counties", sorted(counties), "counties"))
        base.to_csv(DATA / "hmda_county_year_baseline.csv", index=False)
        tables["hmda_county_year_baseline"] = base
        year = year.merge(baseline_compare(apps, base, bank_eth), on=["cert", "activity_year"], how="left")
    tables["bank_hmda_year"] = year
    year.to_csv(DATA / "bank_hmda_year.csv", index=False)
    sub.to_csv(DATA / "bank_hmda_subgroup_year.csv", index=False)
    print(f"applications: {len(apps):,} for {apps.cert.nunique()} banks; bank-years: {len(year):,}")

    db_path = DATA / "ethnic_banks.sqlite"
    if db_path.exists():
        with closing(sqlite3.connect(db_path)) as con, con:
            for name, df in tables.items():
                df.to_sql(name, con, if_exists="replace", index=False)
        print(f"added {', '.join(tables)} to {db_path}")

    last = year[year.activity_year == year.activity_year.max()].merge(banks[["cert", "ethnic_group"]], on="cert")
    cols = ["coethnic_orig_share"] + [c for c in ("expected_coethnic_share_bc",) if c in last]
    print(f"\nCo-ethnic share of originations by FDIC group, {year.activity_year.max()}:")
    print(last.groupby("ethnic_group")[cols].mean().round(3).to_string())


if __name__ == "__main__":
    main()
