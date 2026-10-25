"""Offline tests for link_borrowers.py (no WRDS). All records are synthetic."""
import math
import unittest

import pandas as pd

import link_borrowers as lb

ts = pd.Timestamp
ETH_ROWS = pd.DataFrame({"user_id": [1, 2, 3], "eth_white": [0.1, 0.9, 0.0], "eth_black": [0.0, 0.0, 0.0],
                         "eth_api": [0.9, 0.1, 0.2], "eth_hispanic": [0.0, 0.0, 0.8], "eth_native": [0.0] * 3,
                         "eth_multiple": [0.0] * 3})


class EntitiesTest(unittest.TestCase):
    def test_ids_and_dedup(self):
        loans = pd.DataFrame({"borrower_id": [7.0, 7.0, 8.0], "borrower_name": ["Acme Foods Inc", "Acme Foods Inc", "Zed"],
                              "borrower_city": ["Irvine", "Irvine", None]})
        e = lb.borrower_entities(loans, "borrower_id")
        self.assertEqual(e.borrower_id.tolist(), ["7", "8"])
        self.assertEqual(list(e.columns), ["borrower_id", "name", "city", "state"])

    def test_match_non_bank_names(self):
        ent = pd.DataFrame({"borrower_id": ["7", "8", "9"], "name": ["Acme Foods Inc", "Lotus Trading", "Common Co"],
                            "city": ["Irvine", "Queens", "Austin"]})
        rv = pd.DataFrame({"rcid": [1, 2, 3, 4], "company": ["Acme Foods", "Lotus Trading LLC", "Common", "Common"],
                           "url": [None] * 4, "hq_city": ["Irvine", "Brooklyn", "Austin", "Austin"],
                           "hq_country": ["United States"] * 4, "naics_code": ["311", "423", None, None]})
        for c in ["year_founded", "ultimate_parent_rcid", "ultimate_parent_company_name"]:
            rv[c] = None
        m = lb.match(ent, rv, id_col="borrower_id", banking=False).set_index("borrower_id")
        self.assertEqual(m.loc["7", "match_tier"], "T2_name_city")
        self.assertEqual(m.loc["8", "match_tier"], "T3_name")   # a non-bank name is allowed when banking=False
        self.assertTrue(pd.isna(m.loc["9", "rcid"]))             # two Revelio firms share the name and city


class BorrowerYearTest(unittest.TestCase):
    def setUp(self):
        self.pos = pd.DataFrame([
            (501, 1, ts("2017-01-01"), None, 6),   # executive
            (501, 2, ts("2017-01-01"), None, 2),
            (501, 3, ts("2018-06-01"), None, 7),   # executive from mid-2018
        ], columns=["rcid", "user_id", "startdate", "enddate", "seniority"])

    def test_exec_and_all(self):
        by = lb.borrower_year(self.pos, ETH_ROWS, range(2017, 2019), senior_min=6).set_index("year")
        self.assertEqual(by.n_employees.tolist(), [2, 3])
        self.assertEqual(by.exec_n_employees.tolist(), [1, 2])
        self.assertAlmostEqual(by.loc[2017, "exec_share_api"], 0.9)
        self.assertAlmostEqual(by.loc[2018, "exec_share_api"], 0.55)

    def test_exec_only_and_no_seniority(self):
        by = lb.borrower_year(self.pos, ETH_ROWS, [2018], senior_min=6, all_employees=False)
        self.assertNotIn("share_api", by)
        self.assertIn("exec_share_api", by)
        by = lb.borrower_year(self.pos.drop(columns="seniority"), ETH_ROWS, [2018], senior_min=6)
        self.assertNotIn("exec_share_api", by)


class CoethnicTest(unittest.TestCase):
    def test_measures(self):
        banks = pd.DataFrame({"cert": [1, 2], "ethnic_group": ["Asian or Pacific Islander American", "Hispanic American"]})
        loans = pd.DataFrame({"cert": [1, 1, 2, 1], "borrower_id": ["7", "7", "7", "8"],
                              "deal_date": ["2019-05-01", "2017-02-01", "2019-05-01", "2019-05-01"]})
        by = pd.DataFrame({"borrower_id": ["7", "7"], "rcid": [501, 501], "year": [2018, 2016],
                           "share_api": [0.4, 0.3], "share_hispanic": [0.1, 0.2],
                           "exec_share_api": [0.55, 0.9], "exec_share_hispanic": [0.4, 0.0]})
        co = lb.coethnic(loans, banks, by, "borrower_id")
        self.assertEqual(co.snapshot_year.tolist(), [2018, 2016, 2018, 2018])
        self.assertEqual(co.coethnic_exec_share.tolist()[:3], [0.55, 0.9, 0.4])  # Asian bank -> api; Hispanic -> hispanic
        self.assertEqual(co.coethnic_share.tolist()[:3], [0.4, 0.3, 0.1])
        self.assertTrue(math.isnan(co.coethnic_exec_share.iloc[3]))              # borrower 8 not in Revelio
        self.assertEqual(lb.GROUP_ETH["Native American or Alaska Native"], "native")


if __name__ == "__main__":
    unittest.main()
