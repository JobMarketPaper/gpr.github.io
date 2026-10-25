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

Run the tests with `python3 -m unittest test_build_db test_link_revelio`.

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
