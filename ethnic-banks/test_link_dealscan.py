"""Offline tests for link_dealscan.py (no WRDS). All records are synthetic."""
import unittest

import pandas as pd

import link_dealscan as ld

BANKS = pd.DataFrame([
    (1, "Alpha Bank, National Association", "ALPHA BANCORP, INC.", "Asian or Pacific Islander American", "CA"),
    (2, "First Community Bank", None, "Black or African American", "GA"),
    (3, "Banco Gamma de Puerto Rico", "GAMMA, INC.", "Hispanic American", "PR"),
    (4, "Delta Bank", None, "Hispanic American", "TX"),
], columns=["cert", "name", "holding_company", "ethnic_group", "state"])

LENDERS = pd.DataFrame([
    # lender_id, lender_name, lender_parent_name, lender_country
    (10.0, "Alpha Bank NA", "Alpha Bancorp Inc", "United States"),
    (11.0, "Alpha Bank", "Alpha Bancorp Inc", "United States"),              # second id, same family
    (20.0, "First Community Bank", "Community Holdings A", "United States"),
    (21.0, "First Community Bank", "Community Holdings B", "United States"),  # same name, other family
    (30.0, "Gamma Bank NY", "Gamma Inc", "United States"),                 # parent-only match
    (40.0, "Delta Bank", None, "Japan"),                                    # foreign lender
], columns=ld.LENDER_COLS)


class ResolveTest(unittest.TestCase):
    def test_resolve(self):
        cols = ld.resolve_columns(["LENDER_NAME", "lpc_tranche_id", "lpc_deal_id", "deal_active_date",
                                   "tranche_amount", "lead_arranger", "country"])
        self.assertEqual(cols["lender_name"], "LENDER_NAME")
        self.assertEqual(cols["lender_id"], "LENDER_NAME")  # falls back to the name
        self.assertEqual(cols["borrower_country"], "country")
        self.assertNotIn("lender_country", cols)
        self.assertEqual(ld.select_list(cols, ["lender_name", "lender_parent_name"]),
                         "LENDER_NAME as lender_name, null as lender_parent_name")

    def test_missing(self):
        with self.assertRaises(SystemExit):
            ld.resolve_columns(["lender_name", "deal_active_date"])


class MatchTest(unittest.TestCase):
    def setUp(self):
        self.m = ld.match_lenders(BANKS, LENDERS).set_index("lender_id")

    def test_name_match_several_ids(self):
        self.assertEqual(self.m.loc["10", "cert"], 1)
        self.assertEqual(self.m.loc["11", "cert"], 1)
        self.assertTrue(self.m.loc[["10", "11"], "accept"].all())
        self.assertTrue(self.m.loc["10", "parent_agrees"])

    def test_shared_name_not_accepted(self):
        self.assertTrue(self.m.loc["20", "name_shared"])
        self.assertFalse(self.m.loc["20", "accept"])
        self.assertFalse(self.m.loc["21", "accept"])

    def test_parent_match(self):
        self.assertEqual(self.m.loc["30", "cert"], 3)
        self.assertEqual(self.m.loc["30", "match_tier"], "D2_parent")
        self.assertTrue(self.m.loc["30", "accept"])

    def test_foreign_lender_dropped(self):
        self.assertNotIn("40", self.m.index)


class BankYearTest(unittest.TestCase):
    def test_summary(self):
        loans = pd.DataFrame([
            # cert, lender_id, deal_id, tranche_id, deal_date, tranche_amount, lead_arranger
            (1, "10", "D1", "T1", "2019-03-01", 100.0, "Yes"),
            (1, "11", "D1", "T1", "2019-03-01", 100.0, "No"),   # same tranche via a second lender id
            (1, "10", "D1", "T2", "2019-03-01", 50.0, "No"),
            (1, "10", "D2", "T3", "2020-07-01", None, "No"),
        ], columns=["cert", "lender_id", "deal_id", "tranche_id", "deal_date", "tranche_amount", "lead_arranger"])
        y = ld.bank_year(loans).set_index("year")
        self.assertEqual(y.n_tranches.to_dict(), {2019: 2, 2020: 1})
        self.assertEqual(y.n_deals.to_dict(), {2019: 1, 2020: 1})
        self.assertEqual(y.n_tranches_lead.to_dict(), {2019: 1, 2020: 0})
        self.assertEqual(y.loc[2019, "tranche_amount_sum"], 150.0)
        self.assertTrue(pd.isna(y.loc[2020, "tranche_amount_sum"]))


if __name__ == "__main__":
    unittest.main()
