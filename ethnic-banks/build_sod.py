"""
build_sod.py
Branch networks of the ethnic banks (FDIC Summary of Deposits) and their exposure to co-ethnic neighborhoods
(Census ACS 5-year table B03002, population by Hispanic origin and race).

1. Branches: every Summary of Deposits record (June 30 of each year, 1994 on) of the banks in
   data/ethnic_banks.csv, from the FDIC API `sod` endpoint.
2. County exposure, each year: the deposit-weighted mean over a bank's branches of each group's share of the
   branch county's population, from the ACS 5-year estimates ending that year (the 2009 release for earlier
   years, the latest release for later ones). own_county_share is the share of the bank's own FDIC group.
3. Tract exposure (--tracts): branch coordinates placed in 2020 census tracts with the Census Geocoder, and the
   same measure from the latest ACS by tract. Tract composition is one cross-section applied to every year.
Groups follow B03002: hispanic (any race); non-Hispanic white, black, native (American Indian or Alaska Native),
api (Asian, Native Hawaiian or Pacific Islander) and multiple (two or more races).

Outputs (public data): data/ethnic_bank_branches.csv     branch x year
                       data/bank_branch_exposure_year.csv cert x year
                       tables branches, branch_exposure_year in data/ethnic_banks.sqlite
Usage: python3 build_sod.py [--tracts]    (needs pandas; set CENSUS_API_KEY to raise the Census rate limit)
"""
import argparse
import os
import sqlite3
import sys
import urllib.error
import urllib.parse
from contextlib import closing
from datetime import date
from pathlib import Path

import pandas as pd

from build_db import GROUP_ETH, OUT_DIR, fetch, get_json

DATA = OUT_DIR
ETH = ["white", "black", "api", "hispanic", "native", "multiple"]
FIRST_ACS = 2009
ACS_VARS = {"total": ["B03002_001E"], "white": ["B03002_003E"], "black": ["B03002_004E"], "native": ["B03002_005E"],
            "api": ["B03002_006E", "B03002_007E"], "multiple": ["B03002_009E"], "hispanic": ["B03002_012E"]}
GEOCODER = "https://geocoding.geo.census.gov/geocoder/geographies/coordinates"
# SOD field -> output column
SOD_FIELDS = {"YEAR": "year", "CERT": "cert", "UNINUMBR": "branch_id", "BRNUM": "branch_number",
              "NAMEBR": "branch_name", "ADDRESBR": "address", "CITYBR": "city", "STALPBR": "state", "ZIPBR": "zip",
              "STCNTYBR": "county_fips", "SIMS_LATITUDE": "lat", "SIMS_LONGITUDE": "lon",
              "DEPSUMBR": "deposits_thous", "BKMO": "main_office", "BRSERTYP": "service_type"}


# ---------- branches ----------
def fetch_sod(certs, api_key=None):
    rows = []
    for i, cert in enumerate(certs, 1):
        found, _ = fetch("sod", f"CERT:{cert}", api_key=api_key, sort_by="YEAR")
        rows += found
        print(f"SOD {i}/{len(certs)}: cert {cert}, {len(found):,} branch-years", flush=True)
    return rows


def branches(raw: pd.DataFrame) -> pd.DataFrame:
    missing = [f for f in SOD_FIELDS if f not in raw]
    if missing:
        print(f"warning: SOD has no {missing}; those columns are left empty", file=sys.stderr)
    b = pd.DataFrame({col: raw[f] if f in raw else None for f, col in SOD_FIELDS.items()})
    for col in ("year", "cert", "branch_id", "branch_number", "main_office"):
        b[col] = pd.to_numeric(b[col], errors="coerce").astype("Int64")
    for col in ("lat", "lon", "deposits_thous"):
        b[col] = pd.to_numeric(b[col], errors="coerce")
    b["county_fips"] = b.county_fips.astype("string").str.replace(r"\.0$", "", regex=True).str.zfill(5)
    b["zip"] = b.zip.astype("string").str.replace(r"\.0$", "", regex=True).str.zfill(5)
    b.loc[(b.lat == 0) & (b.lon == 0), ["lat", "lon"]] = None
    return b.drop_duplicates(["cert", "year", "branch_id"]).sort_values(["cert", "year", "branch_id"])


# ---------- Census ----------
def census_get(year, params):
    key = os.environ.get("CENSUS_API_KEY")
    q = urllib.parse.urlencode({**params, **({"key": key} if key else {})}, safe=":*,")
    return get_json(f"https://api.census.gov/data/{year}/acs/acs5?{q}")


def latest_acs_year():
    for y in range(date.today().year - 1, FIRST_ACS - 1, -1):
        try:
            census_get(y, {"get": "NAME", "for": "us:1"})
            return y
        except (urllib.error.URLError, ValueError):
            continue
    raise SystemExit("Could not reach the Census ACS API.")


def acs_shares(rows, geo_cols) -> pd.DataFrame:
    """Census API rows (header first) -> geoid, population and each group's share of it."""
    df = pd.DataFrame(rows[1:], columns=rows[0])
    out = pd.DataFrame({"geoid": df[geo_cols].astype(str).agg("".join, axis=1)})
    num = {v: pd.to_numeric(df[v], errors="coerce") for vs in ACS_VARS.values() for v in vs}
    total = num[ACS_VARS["total"][0]]
    out["population"] = total
    for g in ETH:
        out[f"share_{g}"] = sum(num[v] for v in ACS_VARS[g]) / total.where(total > 0)
    return out


def acs_counties(year):
    get = ",".join(v for vs in ACS_VARS.values() for v in vs)
    return acs_shares(census_get(year, {"get": get, "for": "county:*"}), ["state", "county"])


def acs_tracts(year, state_fips):
    get = ",".join(v for vs in ACS_VARS.values() for v in vs)
    return acs_shares(census_get(year, {"get": get, "for": "tract:*", "in": f"state:{state_fips}"}),
                      ["state", "county", "tract"])


def geocode_tract(lat, lon):
    q = urllib.parse.urlencode({"x": lon, "y": lat, "benchmark": "Public_AR_Current",
                                "vintage": "Census2020_Current", "format": "json"})
    geos = get_json(f"{GEOCODER}?{q}")["result"]["geographies"]
    tracts = next((v for k, v in geos.items() if "tract" in k.lower()), [])
    return tracts[0]["GEOID"] if tracts else None


def tracts_for(points: pd.DataFrame, cache_path: Path) -> pd.DataFrame:
    """lat, lon -> 2020 tract GEOID, cached in cache_path."""
    cache = pd.read_csv(cache_path, dtype={"tract": str}) if cache_path.exists() else pd.DataFrame(
        {"lat": pd.Series(dtype=float), "lon": pd.Series(dtype=float), "tract": pd.Series(dtype=str)})
    todo = points.merge(cache[["lat", "lon"]], how="left", indicator=True).query("_merge == 'left_only'")
    new = []
    for i, (lat, lon) in enumerate(zip(todo.lat, todo.lon), 1):
        try:
            new.append((lat, lon, geocode_tract(lat, lon)))
        except (urllib.error.URLError, ValueError, KeyError) as e:
            print(f"warning: geocoding ({lat}, {lon}) failed: {e}", file=sys.stderr)
        if i % 200 == 0 or i == len(todo):
            print(f"geocoded {i:,}/{len(todo):,}", flush=True)
            cache = pd.concat([cache, pd.DataFrame(new, columns=cache.columns)], ignore_index=True)
            cache.to_csv(cache_path, index=False)
            new = []
    return cache


# ---------- exposure ----------
def exposure(br: pd.DataFrame, shares: pd.DataFrame, geo_col: str, prefix: str, banks: pd.DataFrame) -> pd.DataFrame:
    """Deposit-weighted mean of geo-level group shares per cert-year (branch-count weights if a bank-year has
    no deposits). `shares` is keyed by geoid (and year if it has one)."""
    keys = ["geoid", "year"] if "year" in shares else ["geoid"]
    cols = [f"share_{g}" for g in ETH]
    d = br.rename(columns={geo_col: "geoid"}).merge(shares, on=keys, how="inner").dropna(subset=cols)
    d["w"] = d.deposits_thous.fillna(0).clip(lower=0)
    zero = d.groupby(["cert", "year"]).w.transform("sum") == 0
    d.loc[zero, "w"] = 1.0
    d[cols] = d[cols].mul(d.w, axis=0)
    g = d.groupby(["cert", "year"])
    out = g[cols].sum().div(g.w.sum(), axis=0)
    out.columns = [f"{prefix}_{c}" for c in cols]
    out[f"{prefix}_n_branches_matched"] = g.size()
    out = out.reset_index().merge(banks[["cert", "ethnic_group"]], on="cert", how="left")
    own = out.ethnic_group.map(GROUP_ETH)
    out[f"own_{prefix}_share"] = pd.Series(float("nan"), index=out.index)
    for e in ETH:
        out[f"own_{prefix}_share"] = out[f"own_{prefix}_share"].mask(own == e, out[f"{prefix}_share_{e}"])
    return out.drop(columns="ethnic_group")


def main(argv=None):
    ap = argparse.ArgumentParser(description="Ethnic-bank branches (FDIC SOD) and co-ethnic neighborhood exposure.")
    ap.add_argument("--banks", type=Path, default=DATA / "ethnic_banks.csv")
    ap.add_argument("--tracts", action="store_true", help="also geocode branches to 2020 tracts (slow, cached)")
    args = ap.parse_args(argv)

    banks = pd.read_csv(args.banks, dtype={"zip": str})
    certs = sorted(banks.cert.dropna().astype(int).unique().tolist())
    try:
        br = branches(pd.DataFrame(fetch_sod(certs, os.environ.get("FDIC_API_KEY"))))
    except RuntimeError as e:
        sys.exit(str(e))
    br.to_csv(DATA / "ethnic_bank_branches.csv", index=False)
    print(f"{len(br):,} branch-years for {br.cert.nunique()} banks, {br.year.min()}-{br.year.max()}")

    latest = latest_acs_year()
    br["acs_year"] = br.year.clip(lower=FIRST_ACS, upper=latest)
    county = []
    for y in sorted(br.acs_year.dropna().unique()):
        county.append(acs_counties(int(y)).assign(acs_year=int(y)))
        print(f"ACS {y} counties", flush=True)
    county = pd.concat(county, ignore_index=True)
    county_by_year = br[["year", "acs_year"]].drop_duplicates().merge(county, on="acs_year").drop(columns="acs_year")
    exp = exposure(br, county_by_year, "county_fips", "county", banks)

    if args.tracts:
        pts = br[["lat", "lon"]].dropna().drop_duplicates()
        tr = tracts_for(pts, DATA / "_geocode_cache.csv")
        br = br.merge(tr, on=["lat", "lon"], how="left")
        states = sorted(br.tract.dropna().str[:2].unique())
        tract_acs = pd.concat([acs_tracts(latest, s) for s in states], ignore_index=True)
        exp = exp.merge(exposure(br, tract_acs, "tract", "tract", banks), on=["cert", "year"], how="outer")
        br.to_csv(DATA / "ethnic_bank_branches.csv", index=False)

    totals = br.groupby(["cert", "year"]).agg(n_branches=("branch_id", "nunique"), deposits_thous=("deposits_thous", "sum"),
                                              n_counties=("county_fips", "nunique")).reset_index()
    exp = totals.merge(exp, on=["cert", "year"], how="left")
    exp.to_csv(DATA / "bank_branch_exposure_year.csv", index=False)
    print(f"bank-years: {len(exp):,}; ACS {FIRST_ACS}-{latest}")

    db_path = DATA / "ethnic_banks.sqlite"
    if db_path.exists():
        with closing(sqlite3.connect(db_path)) as con, con:
            br.to_sql("branches", con, if_exists="replace", index=False)
            exp.to_sql("branch_exposure_year", con, if_exists="replace", index=False)
        print(f"added branches, branch_exposure_year to {db_path}")

    last = exp[exp.year == exp.year.max()].merge(banks[["cert", "ethnic_group"]], on="cert")
    print(f"\nMean own-group county share by FDIC group, {exp.year.max()}:")
    print(last.groupby("ethnic_group")["own_county_share"].mean().round(3).to_string())


if __name__ == "__main__":
    main()
