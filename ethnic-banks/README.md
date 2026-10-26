# Ethnic banks database

A database of U.S. ethnic (minority-owned) banks, built from FDIC data.

**Definition.** A bank counts as an ethnic bank if the FDIC designates it a
[Minority Depository Institution (MDI)](https://www.fdic.gov/minority-depository-institutions-program/minority-depository-institutions-list):
an insured depository institution where either

1. 51% or more of the voting stock is owned by minority individuals (**ownership**), or
2. a majority of the board is minority and the community it serves is predominantly minority (**board_and_community**).

The FDIC groups MDIs by minority status code ([FIL-24-2022, attachment](https://www.fdic.gov/news/financial-institution-letters/2022/fil22024a.pdf)):

| Code | Ethnic group | Basis |
|---|---|---|
| 1 / 6 | Black or African American | ownership / board_and_community |
| 2 / 7 | Hispanic American | ownership / board_and_community |
| 3 / 8 | Asian or Pacific Islander American | ownership / board_and_community |
| 4 / 9 | Native American or Alaska Native | ownership / board_and_community |
| 5 / 10 | Multi-racial | ownership / board_and_community |

**Source.** [FDIC BankFind Suite API](https://api.fdic.gov/banks/docs/), `institutions`
endpoint, fields `MDI_STATUS_CODE` and `MDI_STATUS_DESC`.

## Build

```sh
python3 build_db.py
```

Standard library only (Python 3.9+). It writes:

- `data/ethnic_banks.csv`: one row per bank (FDIC certificate number)
- `data/ethnic_banks.sqlite`: tables `banks`, `mdi_status_codes`, `build_info`, and view `active_summary`

If the API is unreachable from your machine, download this URL in a browser
and build from the file:

```
https://api.fdic.gov/banks/institutions?filters=MDI_STATUS_CODE:(1+OR+2+OR+3+OR+4+OR+5+OR+6+OR+7+OR+8+OR+9+OR+10)&fields=CERT,NAME,MDI_STATUS_CODE,MDI_STATUS_DESC,ACTIVE,CITY,STALP,COUNTY,ZIP,ADDRESS,CBSA,ASSET,DEP,REPDTE,OFFICES,ESTYMD,ENDEFYMD,BKCLASS,CHRTAGNT,REGAGNT,FED_RSSD,NAMEHCR,RSSDHCR,WEBADDR&limit=10000&format=csv&download=true&filename=fdic_mdi_institutions
```

```sh
python3 build_db.py --from-file ~/Downloads/fdic_mdi_institutions.csv
```

Run the tests with
`python3 -m unittest test_build_db test_link_revelio test_link_dealscan test_link_borrowers test_build_sod test_build_hmda`.

## Branches and co-ethnic neighborhoods (FDIC SOD + Census ACS)

`build_sod.py` pulls every Summary of Deposits record (branch deposits each
June 30, 1994 on) for the banks, and measures how far each bank's deposits sit
in co-ethnic places, from ACS 5-year table B03002 (population by Hispanic
origin and race):

```sh
python3 build_sod.py            # county exposure, every year
python3 build_sod.py --tracts   # also tract exposure (geocodes branches; slow the first time, then cached)
```

- `county_share_<group>`: deposit-weighted mean, over the bank's branches, of
  the group's share of the branch county's population, using the ACS release
  ending that year (2009 release for earlier years, latest for later ones).
  `own_county_share` is the bank's own FDIC group. Groups: `hispanic` (any
  race), and non-Hispanic `white`, `black`, `native`, `api` (Asian + Pacific
  Islander), `multiple`.
- `tract_share_<group>` / `own_tract_share` (`--tracts`): the same with 2020
  census tracts (Census Geocoder on the SOD coordinates) and the latest ACS;
  tract composition is one cross-section applied to every year.
- Outputs: `data/ethnic_bank_branches.csv` (branch x year),
  `data/bank_branch_exposure_year.csv` (bank x year). Public data.
- Connecticut replaced its counties with planning regions in the 2022 ACS, so
  Connecticut branches may lose county matches from then on.

## Mortgage lending (HMDA)

`build_hmda.py` pulls the banks' HMDA applications (2018 on) from the CFPB
Data Browser API, with each applicant's own race and ethnicity, including the
detailed groups HMDA has collected since 2018 (Chinese, Korean, Vietnamese,
Filipino, Asian Indian, Japanese; Mexican, Puerto Rican, Cuban; Native
Hawaiian, Guamanian or Chamorro, Samoan).

```sh
python3 build_hmda.py --match-only --panel 2022_public_panel.csv 2023_public_panel.csv
python3 build_hmda.py --use-existing-match --baseline-counties 10
```

- **Lender link:** HMDA identifies lenders by LEI. The HMDA Reporter Panel
  (CSV from the FFIEC data publication pages, 2018-2023; not produced from
  2024) maps LEI to `respondent_rssd` = FDIC `fed_rssd`. Banks it misses, or
  without `--panel`, are matched by standardized name to the Data Browser filer
  list; names shared by several LEIs get `accept = False`. Review
  `data/bank_hmda_lei_match.csv`.
- **Applicant group** from the applicant's own codes (co-applicant ignored),
  on the same groups as the ACS measures: `hispanic` if any Hispanic code;
  otherwise the minority race, `multiple` for two or more minority races,
  `white` if only white. `applicant_subgroup` is the detailed group;
  `coethnic` = 1 when the applicant's group is the bank's.
- **Outputs:** `data/bank_hmda_applications.parquet` (loan level),
  `data/bank_hmda_year.csv` (applications, originations, denial rate, shares of
  originations by group, `coethnic_orig_share`, denial rates for co-ethnic and
  other applicants), `data/bank_hmda_subgroup_year.csv` (originations by
  detailed group).
- **Baseline** (`--baseline-counties N`): all lenders' applications in the N
  counties where the banks take the most applications.
  `expected_coethnic_share_bc` is the co-ethnic share implied by all lenders'
  originations in the same county-years; compare it with the banks'
  `coethnic_share_bc` there. These files are large (all lenders in big
  counties) and cached in `data/_hmda_raw/`.
- HMDA before 2018 has a different format and no detailed groups; it is not
  pulled.

## Link to Revelio Labs (WRDS)

`link_revelio.py` matches each bank to its Revelio company (`rcid`) and builds
bank-year workforce ethnicity shares. Run it where WRDS is reachable, after
`build_db.py` (needs `pandas`, `pyarrow`, `wrds`; set `WRDS_USERNAME` or let
`wrds` prompt):

```sh
python3 link_revelio.py --match-only          # 1. match, then review data/bank_revelio_match.csv
python3 link_revelio.py --use-existing-match  # 2. pull positions and ethnicity for the reviewed matches
```

- **Matching** (`revelio_common.company_mapping`), in order: website domain;
  standardized name + HQ city; standardized name alone among U.S. banking firms
  (NAICS 522 or a bank-like name). Name matches must be unique on both sides.
  Check the `T2`/`T3` rows, `rcid_shared` and `chk_city_agree` by hand; fix
  `rcid` in the CSV where needed before step 2.
- **Workforce** (`revelio.individual_positions` joined to Revelio's user
  table): U.S. employees on December 31 of each year from 2008, counted once
  per bank. `share_white`, `share_black`, `share_api`, `share_hispanic`,
  `share_native`, `share_multiple` are means of Revelio's predicted ethnicity
  probabilities; `n_with_ethnicity` is the number of employees with a
  prediction. A second file breaks this down by Revelio seniority level.
- The script prints mean shares by FDIC group for the latest year as a sanity
  check (Asian American MDIs should show a high `share_api`, and so on).

Outputs: `data/bank_revelio_match.csv`, `data/bank_workforce_ethnicity_year.csv`,
`data/bank_workforce_ethnicity_year_seniority.csv`, and the same tables in
`data/ethnic_banks.sqlite`. These are derived from licensed Revelio data and
are git-ignored; do not commit them to this public repository.

## Link to DealScan (WRDS)

`link_dealscan.py` finds the banks among LSEG DealScan lenders
(`dealscan.dealscan`, one row per tranche x lender) and pulls their syndicated
loan participations. Same two-step flow:

```sh
python3 link_dealscan.py --match-only          # 1. review data/bank_dealscan_lender_match.csv
python3 link_dealscan.py --use-existing-match  # 2. pull loans for rows with accept = True
```

- **Matching**, U.S. lenders only (when DealScan reports a lender country):
  standardized lender name = bank name (a bank may have several DealScan lender
  ids); else lender parent = the bank's FDIC holding company, unique among the
  banks. `accept` is False when the name is used by several DealScan lender ids
  that no single parent ties together (e.g. "First Community Bank"); check
  those by hand, and flip `accept` for any other row you reject.
- **Outputs:** `data/bank_dealscan_loans.csv` (every tranche x lender row for
  accepted lenders, with borrower name, location and SIC, amounts, spread,
  maturity, role and lead-arranger flag) and `data/bank_dealscan_year.csv`
  (per bank-year: tranches, deals, tranches as lead arranger, total tranche
  amount as reported by DealScan, each tranche counted once).
- DealScan column names are looked up on WRDS at run time; the script prints
  the mapping it used and stops with the full column list if a required
  column is missing.
- Outputs are licensed DealScan data and git-ignored.

## Borrower ethnicity and co-ethnic lending

`link_borrowers.py` matches DealScan borrowers to Revelio companies and
measures how far each loan of an ethnic bank goes to a borrower of the
bank's own group. Run it after `link_dealscan.py`:

```sh
python3 link_borrowers.py --match-only          # review data/dealscan_borrower_revelio_match.csv
python3 link_borrowers.py --use-existing-match  # workforce, shares, co-ethnic measures
```

- **Borrowers:** by default, those in the ethnic banks' loans. Add `--all-us`
  for every U.S. DealScan borrower (executives only), as a baseline for
  comparing ethnic banks with other lenders; its files end in `_all_us`.
- **Matching:** standardized name + HQ city, then standardized name alone
  among U.S. Revelio companies, unique on both sides. Pass
  `--company-mapping <folder>` to reuse a local parquet copy of
  `company_mapping` instead of pulling U.S. companies from WRDS.
- **Ethnicity:** borrower-year shares on December 31 among executives
  (`exec_share_*`, Revelio seniority >= `--senior-min`, default 6 = executive
  and senior executive; the script prints the seniority range it finds) and
  among all U.S. employees (`share_*`).
- **Co-ethnic measures** (`data/bank_dealscan_loans_coethnic.csv`): for each
  loan, the borrower's share of the lending bank's group (Asian American bank
  -> `api`, Black -> `black`, Hispanic -> `hispanic`, Native American ->
  `native`, multi-racial -> `multiple`) among executives
  (`coethnic_exec_share`) and all employees (`coethnic_share`), taken at the
  December 31 before the loan (`snapshot_year`). Loans before Revelio
  coverage have no measure.
- Name-based ethnicity is a prediction, and DealScan's U.S. borrowers skew to
  large firms; treat the shares as noisy measures, not classifications.

## Columns (`banks`)

| Column | FDIC field | Notes |
|---|---|---|
| `cert` | CERT | FDIC certificate number (primary key) |
| `name` | NAME | |
| `ethnic_group` | derived | From `mdi_status_desc`, else `mdi_status_code` |
| `mdi_basis` | derived | `ownership` or `board_and_community` |
| `mdi_status_code`, `mdi_status_desc` | MDI_STATUS_CODE, MDI_STATUS_DESC | As published by the FDIC |
| `active` | ACTIVE | 1 = open and insured today |
| `city`, `state`, `county`, `zip`, `address`, `cbsa` | CITY, STALP, COUNTY, ZIP, ADDRESS, CBSA | Headquarters location |
| `total_assets_thous`, `total_deposits_thous` | ASSET, DEP | Thousands of dollars |
| `financials_as_of` | REPDTE | Report date of the asset and deposit figures |
| `offices` | OFFICES | Number of domestic offices |
| `established`, `end_date` | ESTYMD, ENDEFYMD | ISO dates; `end_date` is empty for open banks |
| `charter_class`, `chartering_agency`, `regulator` | BKCLASS, CHRTAGNT, REGAGNT | |
| `fed_rssd` | FED_RSSD | Federal Reserve ID, for joining to Call Report / FR Y-9C data |
| `holding_company`, `holding_company_rssd` | NAMEHCR, RSSDHCR | Top regulatory holder |
| `website` | WEBADDR | |

## Caveats

- **Current status only.** The MDI flag is the FDIC's current (or, for closed
  banks, last recorded) designation. It does not tell you when a bank became
  or stopped being an MDI; the FDIC's quarterly historical MDI lists are the
  source for a panel.
- **Broad groups.** The FDIC does not split "Asian or Pacific Islander
  American" (or other groups) into national origins such as Chinese, Korean or
  Vietnamese American. Revelio's ethnicity predictions use the same broad
  groups.
- **Revelio coverage.** Revelio sees employees with online profiles, which
  under-covers branch and back-office staff at small banks; read
  `n_employees` next to the FDIC office count before using shares for small
  banks.
- **MDIs only.** Banks that serve an ethnic community without minority
  ownership or a minority board are not designated MDIs and are not included.
  Credit unions (NCUA) are not included either.
- The build prints a warning, and records it in `build_info`, for any
  institution whose status code and description disagree or whose code it does
  not recognize.
