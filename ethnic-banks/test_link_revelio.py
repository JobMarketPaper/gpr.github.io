"""Offline tests for link_revelio.py (no WRDS). All records are synthetic."""
import math
import unittest

import pandas as pd

import link_revelio as lr

BANKS = pd.DataFrame([
    # cert, name, website, city, ethnic_group
    (1, "Alpha Bank, National Association", "https://www.alphabank.com/home", "Los Angeles", "Asian or Pacific Islander American"),
    (2, "The Beta Savings Bank, F.S.B.", None, "Chicago", "Black or African American"),
    (3, "Gamma Bank & Trust Co.", None, "Miami", "Hispanic American"),
    (4, "First Bank", None, "Dallas", "Hispanic American"),      # name shared by two Revelio firms
    (5, "Delta Bank", "deltabank.com", "Houston", "Asian or Pacific Islander American"),
], columns=["cert", "name", "website", "city", "ethnic_group"])

RV = pd.DataFrame([
    # rcid, company, url, hq_city, hq_country, naics_code
    (101, "Alpha Bank", "alphabank.com", "Los Angeles", "United States", "522110"),
    (102, "Alpha Financial Holdings", "alphabank.com", "Los Angeles", "United States", "551111"),
    (201, "Beta Savings Bank", "betasavings.com", "Chicago", "United States", "522120"),
    (301, "Gamma Bank and Trust", "gammabank.com", "Coral Gables", "United States", "522110"),
    (401, "First Bank", "firstbank1.com", "Dallas", "United States", "522110"),
    (402, "First Bank", "firstbank2.com", "Dallas", "United States", "522110"),
    (501, "Delta Bancorp", "deltabank.com", "Austin", "United States", "551111"),
    (502, "Delta Group", "deltabank.com", "Seattle", "United States", None),
], columns=["rcid", "company", "url", "hq_city", "hq_country", "naics_code"])
for col in ["year_founded", "ultimate_parent_rcid", "ultimate_parent_company_name"]:
    RV[col] = None


class NormalizeTest(unittest.TestCase):
    def test_names(self):
        got = lr.norm_name(pd.Series(["Alpha Bank, National Association", "The Beta Savings Bank, F.S.B.",
                                      "Gamma Bank & Trust Co.", "Banco Uno, N.A.", None]))
        self.assertEqual(got.tolist()[:4], ["alpha bank", "beta savings bank", "gamma bank and trust", "banco uno"])
        self.assertTrue(pd.isna(got.iloc[4]))

    def test_domains(self):
        got = lr.norm_domain(pd.Series(["https://www.alphabank.com/home", "WWW.ExampleBank.COM:443", "facebook.com/x",
                                        "", None]))
        self.assertEqual(got.tolist()[:2], ["alphabank.com", "examplebank.com"])
        self.assertTrue(all(pd.isna(v) for v in got.tolist()[2:]))


class MatchTest(unittest.TestCase):
    def setUp(self):
        self.m = lr.match(BANKS, RV).set_index("cert")

    def test_domain_tie_broken_by_name(self):
        self.assertEqual(self.m.loc[1, "rcid"], 101)
        self.assertEqual(self.m.loc[1, "match_tier"], "T1_domain")
        self.assertEqual(self.m.loc[1, "n_cand"], 2)

    def test_name_city(self):
        self.assertEqual(self.m.loc[2, "rcid"], 201)
        self.assertEqual(self.m.loc[2, "match_tier"], "T2_name_city")

    def test_name_only(self):
        self.assertEqual(self.m.loc[3, "rcid"], 301)
        self.assertEqual(self.m.loc[3, "match_tier"], "T3_name")
        self.assertEqual(self.m.loc[3, "chk_city_agree"], False)

    def test_ambiguous_names_left_unmatched(self):
        self.assertTrue(pd.isna(self.m.loc[4, "rcid"]))

    def test_unresolved_domain_tie_left_unmatched(self):
        # two Revelio firms share the domain and neither name nor city agrees
        self.assertTrue(pd.isna(self.m.loc[5, "rcid"]))

    def test_one_row_per_bank(self):
        self.assertEqual(len(self.m), len(BANKS))
        self.assertFalse(self.m.rcid_shared.any())


class EthnicityTest(unittest.TestCase):
    def test_pick_prob(self):
        kind, spec = lr.pick_ethnicity(["user_id", "white_prob", "black_prob", "api_prob", "hispanic_prob",
                                        "native_prob", "multiple_prob", "ethnicity_predicted"])
        self.assertEqual(kind, "prob")
        self.assertEqual(spec["api"], "api_prob")

    def test_pick_label(self):
        self.assertEqual(lr.pick_ethnicity(["user_id", "Ethnicity_Predicted"]), ("label", "Ethnicity_Predicted"))
        with self.assertRaises(SystemExit):
            lr.pick_ethnicity(["user_id", "sex"])

    def test_label_one_hot(self):
        users = pd.DataFrame({"user_id": [1, 2, 3], "eth": ["API", "hispanic", None]})
        e = lr.ethnicity_frame(users, "label", "eth").set_index("user_id")
        self.assertEqual(e.loc[1, "eth_api"], 1.0)
        self.assertEqual(e.loc[2, "eth_api"], 0.0)
        self.assertTrue(math.isnan(e.loc[3, "eth_api"]))


class AnnualSharesTest(unittest.TestCase):
    def test_snapshot(self):
        ts = pd.Timestamp
        pos = pd.DataFrame([
            (101, 1, ts("2018-03-01"), None, 1),
            (101, 1, ts("2019-01-01"), None, 4),            # second position, same user and bank
            (101, 2, ts("2018-06-01"), ts("2019-06-30"), 1),
            (101, 3, ts("2020-02-01"), None, 1),            # no ethnicity record
        ], columns=["rcid", "user_id", "startdate", "enddate", "seniority"])
        eth = pd.DataFrame({"user_id": [1, 2], "eth_white": [0.2, 0.6], "eth_black": [0.0, 0.0],
                            "eth_api": [0.8, 0.0], "eth_hispanic": [0.0, 0.4], "eth_native": [0.0, 0.0],
                            "eth_multiple": [0.0, 0.0]})
        out = lr.annual_shares(pos, eth, range(2018, 2021)).set_index("year")
        self.assertEqual(out.n_employees.tolist(), [2, 1, 2])  # user 2 left mid-2019; user 3 joined 2020
        self.assertEqual(out.n_with_ethnicity.tolist(), [2, 1, 1])
        self.assertAlmostEqual(out.loc[2018, "share_api"], 0.4)
        self.assertAlmostEqual(out.loc[2020, "share_api"], 0.8)

        sen = lr.annual_shares(pos, eth, [2019], by=["seniority"]).set_index("seniority")
        self.assertEqual(sen.n_employees.to_dict(), {1: 1, 4: 1})


if __name__ == "__main__":
    unittest.main()
