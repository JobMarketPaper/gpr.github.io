"""
link_revelio.py
Link the ethnic-bank database to Revelio Labs (via WRDS) and build bank-year workforce ethnicity shares.

1. Match each bank (FDIC cert) to a Revelio company (rcid) in revelio_common.company_mapping.
   Tiers, applied in order, each only to banks still unmatched:
     T1  website domain (exact); ties broken by name, then HQ city, then NAICS 522
     T2  standardized name + HQ city, unique on both sides
     T3  standardized name alone, unique on both sides among U.S. banking firms
2. Pull U.S. positions at the matched rcids (revelio.individual_positions) and each employee's predicted
   ethnicity (Revelio's user table), in checkpointed batches. Built bottom-up from positions because
   Revelio's workforce tables cover mostly larger companies and most MDIs are small.
3. Headcount on December 31 of each year = employees with startdate <= Dec 31 and (enddate null or
   >= Dec 31), counted once per bank. Ethnicity shares are means of Revelio's ethnicity probabilities
   (white, black, api, hispanic, native, multiple), or of one-hot labels if only a label is available.

Inputs : data/ethnic_banks.csv (from build_db.py)
Outputs: data/bank_revelio_match.csv                         one row per cert; rcid may be missing
         data/bank_workforce_ethnicity_year.csv              cert x year
         data/bank_workforce_ethnicity_year_seniority.csv    cert x year x seniority (if Revelio has seniority)
         the same three tables in data/ethnic_banks.sqlite
Run where WRDS is reachable (needs pandas, pyarrow, wrds). Set WRDS_USERNAME or let wrds prompt.
Review the match file (T2/T3 especially), edit rcids by hand if needed, then rerun with --use-existing-match.
Revelio-derived outputs are licensed data: they are git-ignored, keep them out of the public repo.
"""
import argparse
import hashlib
import os
import re
import sqlite3
import sys
import unicodedata
from contextlib import closing
from pathlib import Path

import pandas as pd

DATA = Path(__file__).resolve().parent / "data"
PARTS = DATA / "_wrds_parts"
FIRST_YEAR = 2008
BATCH_RCID, BATCH_USER = 50, 5000
US = ["United States", "Puerto Rico", "Guam", "American Samoa", "Northern Mariana Islands", "U.S. Virgin Islands"]
ETH = ["white", "black", "api", "hispanic", "native", "multiple"]
ETH_LABELS = {"white": "white", "black": "black", "african american": "black", "api": "api", "asian": "api",
              "asian or pacific islander": "api", "pacific islander": "api", "hispanic": "hispanic",
              "latino": "hispanic", "native": "native", "native american": "native", "multiple": "multiple",
              "multi": "multiple", "multiracial": "multiple"}
RV_COLS = ("rcid, company, url, year_founded, hq_city, hq_country, ultimate_parent_rcid, "
           "ultimate_parent_company_name, naics_code")

LEGAL_RE = re.compile(r"\b(national association|n a|f s b|s s b|fsb|ssb|inc|incorporated|corp|corporation|co|"
                      r"company|llc|ltd|limited|the)\b")
BANKLIKE_RE = re.compile(r"\b(?:bank|banco|bancorp|savings|trust)\b")
GENERIC_DOMAINS = {"linkedin.com", "facebook.com", "instagram.com", "twitter.com", "x.com", "google.com",
                   "sites.google.com", "youtube.com", "about.me"}


def norm_domain(u: pd.Series) -> pd.Series:
    s = u.fillna("").astype(str).str.strip().str.lower()
    s = s.str.replace(r"^[a-z]+://", "", regex=True).str.replace(r"^www\d?\.", "", regex=True)
    s = s.str.split(r"[/?#:]", n=1, regex=True).str[0].str.strip(".")
    return s.where(s.str.contains(r"\.", regex=True) & ~s.isin(GENERIC_DOMAINS))


def norm_name(u: pd.Series) -> pd.Series:
    def one(x):
        if not isinstance(x, str) or not x.strip():
            return None
        x = unicodedata.normalize("NFKD", x).encode("ascii", "ignore").decode().lower()
        x = re.sub(r"\(.*?\)", " ", x).replace("&", " and ")
        x = re.sub(r"\s+", " ", re.sub(r"[^a-z0-9 ]", " ", x))
        x = re.sub(r"\s+", " ", LEGAL_RE.sub(" ", x)).strip()
        return x or None
    return u.map(one)


def norm_city(u: pd.Series) -> pd.Series:
    s = u.fillna("").astype(str).str.lower().str.replace(r"[^a-z ]", " ", regex=True)
    s = s.str.replace(r"\s+", " ", regex=True).str.strip()
    return s.where(s != "")


# ---------- matching ----------
def match(banks: pd.DataFrame, rv: pd.DataFrame, id_col="cert", banking=True) -> pd.DataFrame:
    """One row per entity (`id_col`) with the matched rcid (or NA), match tier and Revelio attributes.

    `banks` needs `id_col`, name and city, and optionally website. With banking=True the name-only tier
    (T3) is limited to Revelio banking firms; otherwise it uses every Revelio company passed in.
    """
    b = banks[[id_col, "name", "city"]].assign(website=banks.get("website")).rename(columns={id_col: "cert"})
    b["domain"], b["name_n"], b["city_n"] = norm_domain(b.website), norm_name(b.name), norm_city(b.city)
    rv = rv.copy()
    rv["rcid"] = rv["rcid"].astype("int64")
    rv["domain"], rv["name_n"], rv["city_n"] = norm_domain(rv.url), norm_name(rv.company), norm_city(rv.hq_city)
    rv["is_bank"] = rv["naics_code"].astype(str).str.startswith("522")

    # T1: domain
    t1 = b.dropna(subset=["domain"]).merge(
        rv.dropna(subset=["domain"])[["rcid", "domain", "name_n", "city_n", "is_bank"]], on="domain",
        suffixes=("", "_rv"))
    t1["name_eq"] = t1.name_n == t1.name_n_rv
    t1["city_eq"] = t1.city_n == t1.city_n_rv
    t1["n_cand"] = t1.groupby("cert")["rcid"].transform("size")
    t1 = t1.sort_values(["cert", "name_eq", "city_eq", "is_bank", "rcid"],
                        ascending=[True, False, False, False, True]).groupby("cert").head(1)
    # keep unique-domain hits, or multi-candidate hits resolved by name or city
    t1 = t1[(t1.n_cand == 1) | t1.name_eq | t1.city_eq].assign(match_tier="T1_domain")
    found = [t1[["cert", "rcid", "match_tier", "n_cand"]]]
    matched = set(t1.cert)

    # T2: name + city; T3: name alone (among banking firms when matching banks). Unique on both sides.
    pool = rv.dropna(subset=["name_n"])
    t3_pool = pool[pool.is_bank | pool.name_n.str.contains(BANKLIKE_RE)] if banking else pool
    for keys, tier, rv_side in [(["name_n", "city_n"], "T2_name_city", pool), (["name_n"], "T3_name", t3_pool)]:
        r = rv_side.dropna(subset=keys)
        r = r[r.groupby(keys)["rcid"].transform("size") == 1]
        left = b[~b.cert.isin(matched)].dropna(subset=keys)
        left = left[left.groupby(keys)["cert"].transform("size") == 1]
        t = left.merge(r[["rcid", *keys]], on=keys).assign(match_tier=tier, n_cand=1)
        found.append(t[["cert", "rcid", "match_tier", "n_cand"]])
        matched |= set(t.cert)

    m = pd.concat(found, ignore_index=True)
    assert m.cert.is_unique
    rv_attr = rv.set_index("rcid")[["company", "url", "hq_city", "hq_country", "naics_code", "year_founded",
                                    "ultimate_parent_rcid", "ultimate_parent_company_name", "city_n"]]
    rv_attr = rv_attr[~rv_attr.index.duplicated()].add_prefix("rv_")
    keep = [c for c in [id_col, "name", "ethnic_group", "active", "city", "state", "website"] if c in banks]
    m = m.rename(columns={"cert": id_col})
    out = banks[keep].merge(m, on=id_col, how="left").merge(rv_attr, left_on="rcid", right_index=True, how="left")
    out["rcid"] = out["rcid"].astype("Int64")
    out["domain"] = norm_domain(out.website) if "website" in out else None
    out["rcid_shared"] = out.rcid.notna() & out.duplicated("rcid", keep=False)
    out["chk_city_agree"] = (norm_city(out.city) == out.rv_city_n).where(out.rv_city_n.notna())
    return out.drop(columns="rv_city_n")


# ---------- ethnicity columns ----------
def pick_ethnicity(cols):
    """('prob', {group: column}) if the user table has ethnicity probabilities, else ('label', column)."""
    lower = {c.lower(): c for c in cols}
    probs = {}
    for g in ETH:
        for cand in (f"{g}_prob", f"prob_{g}", f"{g}_probability", f"ethnicity_{g}_prob"):
            if cand in lower:
                probs[g] = lower[cand]
                break
    if probs:
        return "prob", probs
    label = next((c for c in cols if "ethnic" in c.lower() or c.lower() in ("race", "race_predicted")), None)
    if label:
        return "label", label
    raise SystemExit(f"No ethnicity columns in the Revelio user table. Columns: {sorted(cols)}")


def ethnicity_frame(users: pd.DataFrame, kind, spec) -> pd.DataFrame:
    """user_id plus eth_<group> columns in [0, 1]."""
    out = users[["user_id"]].copy()
    if kind == "prob":
        for g in ETH:
            out[f"eth_{g}"] = pd.to_numeric(users[spec[g]], errors="coerce") if g in spec else float("nan")
    else:
        lab = users[spec].astype("string").str.lower().str.strip().map(ETH_LABELS)
        for g in ETH:
            out[f"eth_{g}"] = (lab == g).astype(float).where(lab.notna())
    return out.drop_duplicates("user_id")


def annual_shares(pos: pd.DataFrame, eth: pd.DataFrame, years, by=()) -> pd.DataFrame:
    """Headcount and mean ethnicity shares per rcid-year (and `by`) on December 31."""
    keys = ["rcid", "user_id", *by]
    snaps = []
    for y in years:
        d = pd.Timestamp(year=y, month=12, day=31)
        a = pos[(pos.startdate <= d) & (pos.enddate.isna() | (pos.enddate >= d))]
        snaps.append(a[keys].drop_duplicates().assign(year=y))
    a = pd.concat(snaps, ignore_index=True).merge(eth, on="user_id", how="left")
    g = a.groupby(["rcid", "year", *by])
    out = g.size().rename("n_employees").to_frame()
    out["n_with_ethnicity"] = g[f"eth_{ETH[0]}"].count()
    shares = g[[f"eth_{e}" for e in ETH]].mean()
    shares.columns = [f"share_{e}" for e in ETH]
    return out.join(shares).reset_index()


# ---------- WRDS ----------
def sql_list(values):
    return ",".join("'" + str(v).replace("'", "''") + "'" for v in values)


def digest(values):
    return hashlib.md5(",".join(map(str, values)).encode()).hexdigest()[:10]


def cached(path: Path, pull):
    if path.exists():
        return pd.read_parquet(path)
    df = pull()
    df.to_parquet(path, index=False)
    return df


def pull_candidates(db, domains):
    us = sql_list(US)
    where = [f"""(hq_country in ({us}) and (cast(naics_code as text) like '522%%'
                 or company ilike any (array['%%bank%%', '%%banco%%', '%%savings%%', '%%trust co%%'])))"""]
    if domains:
        where.append(f"url ilike any (array[{sql_list(f'%%{d}%%' for d in domains)}])")
    return db.raw_sql(f"select {RV_COLS} from revelio_common.company_mapping where " + " or ".join(where))


def pull_batches(db, ids, size, prefix, query):
    """Run `query(id_list_sql)` per batch, checkpointing each batch by its content."""
    parts = []
    for i in range(0, len(ids), size):
        batch = ids[i:i + size]
        path = PARTS / f"{prefix}_{digest(batch)}.parquet"
        q = ",".join(map(str, batch)) if pd.api.types.is_number(batch[0]) else sql_list(batch)
        parts.append(cached(path, lambda: db.raw_sql(query(q))))
        print(f"{prefix} batch {i // size + 1}/{-(-len(ids) // size)}: {len(parts[-1]):,} rows", flush=True)
    return pd.concat(parts, ignore_index=True) if parts else pd.DataFrame()


def user_table(db):
    tables = db.list_tables(library="revelio")
    if "individual_user" in tables:
        return "individual_user"
    cand = [t for t in tables if "user" in t and not any(x in t for x in ("education", "skill", "position"))]
    if not cand:
        raise SystemExit(f"No Revelio user table found in library 'revelio': {sorted(tables)}")
    return cand[0]


def pull_workforce(db, rcids, senior_min=None):
    """U.S. positions at `rcids` and each employee's ethnicity, as (pos, eth, has_seniority).

    With `senior_min`, only positions at Revelio seniority >= senior_min are pulled.
    """
    has_seniority = "seniority" in set(db.describe_table(library="revelio", table="individual_positions")["name"])
    if senior_min is not None and not has_seniority:
        raise SystemExit("revelio.individual_positions has no seniority column, so executives cannot be selected.")
    senior_sql = f" and seniority >= {int(senior_min)}" if senior_min is not None else ""
    prefix = "pos" if senior_min is None else f"pos_sen{int(senior_min)}"
    pos = pull_batches(db, rcids, BATCH_RCID, prefix, lambda q: f"""
        select rcid, user_id, startdate, enddate{', seniority' if has_seniority else ''}
        from revelio.individual_positions
        where rcid in ({q}) and startdate is not null{senior_sql}
          and (country in ({sql_list(US)}) or country is null)""")
    if pos.empty:
        raise SystemExit("No Revelio positions found for the matched rcids.")

    ut = user_table(db)
    kind, spec = pick_ethnicity(list(db.describe_table(library="revelio", table=ut)["name"]))
    print(f"ethnicity from revelio.{ut}: {kind} {spec}")
    cols = list(spec.values()) if kind == "prob" else [spec]
    uids = pos.user_id.dropna()
    if pd.api.types.is_numeric_dtype(uids):
        uids = uids.astype("int64")  # "123.0" in an IN list would defeat the user_id index
    users = pull_batches(db, sorted(uids.unique().tolist()), BATCH_USER, f"user_{ut}",
                         lambda q: f"select user_id, {', '.join(cols)} from revelio.{ut} where user_id in ({q})")

    pos["rcid"] = pos["rcid"].astype("int64")
    pos["startdate"], pos["enddate"] = pd.to_datetime(pos.startdate), pd.to_datetime(pos.enddate)
    return pos, ethnicity_frame(users, kind, spec), has_seniority


def snapshot_years(pos):
    """FIRST_YEAR through the last complete year of Revelio position data."""
    end = pos.startdate.max()
    return range(FIRST_YEAR, end.year + (end.month == 12))


def main(argv=None):
    ap = argparse.ArgumentParser(description="Link ethnic banks to Revelio and build workforce ethnicity shares.")
    ap.add_argument("--banks", type=Path, default=DATA / "ethnic_banks.csv")
    ap.add_argument("--match-only", action="store_true", help="stop after writing the match file for review")
    ap.add_argument("--use-existing-match", action="store_true",
                    help="skip matching and use data/bank_revelio_match.csv as edited")
    args = ap.parse_args(argv)

    import wrds
    sys.stdout.reconfigure(encoding="utf-8")
    PARTS.mkdir(parents=True, exist_ok=True)
    banks = pd.read_csv(args.banks, dtype={"zip": str})
    match_path = DATA / "bank_revelio_match.csv"
    db = wrds.Connection(wrds_username=os.environ.get("WRDS_USERNAME"))
    try:
        if args.use_existing_match:
            m = pd.read_csv(match_path, dtype={"rcid": "Int64"})
        else:
            domains = sorted(set(norm_domain(banks.website).dropna()))
            rv = cached(PARTS / f"rv_candidates_{digest(domains)}.parquet", lambda: pull_candidates(db, domains))
            print(f"Revelio candidate companies: {len(rv):,}")
            m = match(banks, rv)
            m.to_csv(match_path, index=False)
            print(f"matched {m.rcid.notna().sum()} of {len(m)} banks -> {match_path}")
            print(m.match_tier.value_counts().to_string())
            print(f"rcid shared by more than one bank: {m.rcid_shared.sum()}; "
                  f"HQ city agrees: {m.chk_city_agree.mean():.1%}")
            if args.match_only:
                return

        rcids = sorted(m.rcid.dropna().astype("int64").unique().tolist())
        if not rcids:
            raise SystemExit(f"No bank is matched to a Revelio rcid in {match_path}.")
        pos, eth, has_seniority = pull_workforce(db, rcids)
    finally:
        db.close()

    years = snapshot_years(pos)
    link = m.dropna(subset=["rcid"])[["cert", "rcid"]].astype("int64")

    tables = {"revelio_match": m}
    year = link.merge(annual_shares(pos, eth, years), on="rcid")
    tables["workforce_ethnicity_year"] = year
    year.to_csv(DATA / "bank_workforce_ethnicity_year.csv", index=False)
    if has_seniority:
        sen = link.merge(annual_shares(pos.dropna(subset=["seniority"]), eth, years, by=["seniority"]), on="rcid")
        tables["workforce_ethnicity_year_seniority"] = sen
        sen.to_csv(DATA / "bank_workforce_ethnicity_year_seniority.csv", index=False)
    print(f"bank-years: {len(year):,} for {year.cert.nunique()} banks, {years.start}-{years.stop - 1}")

    db_path = DATA / "ethnic_banks.sqlite"
    if db_path.exists():
        with closing(sqlite3.connect(db_path)) as con, con:
            for name, df in tables.items():
                df.to_sql(name, con, if_exists="replace", index=False)
        print(f"added {', '.join(tables)} to {db_path}")

    # Sanity check: workforce shares by FDIC group in the latest year
    if "ethnic_group" in banks:
        last = year[year.year == year.year.max()].merge(banks[["cert", "ethnic_group"]], on="cert")
        share_cols = [f"share_{e}" for e in ETH]
        print(f"\nMean workforce shares by FDIC MDI group, {year.year.max()}:")
        print(last.groupby("ethnic_group")[share_cols].mean().round(2).to_string())


if __name__ == "__main__":
    main()
