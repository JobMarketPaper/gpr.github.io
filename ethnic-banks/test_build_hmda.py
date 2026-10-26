"""Offline tests for build_hmda.py (no HMDA API calls). All records are synthetic."""
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import pandas as pd

import build_hmda as bh


def app(race=(), eth=(2,), action=1, cert=1, county="06037", year=2022):
    r = list(race) + [None] * (5 - len(race))
    e = list(eth) + [None] * (5 - len(eth))
    return {"cert": cert, "activity_year": year, "county_code": county, "action_taken": action,
            **dict(zip(bh.RACE_COLS, r)), **dict(zip(bh.ETHN_COLS, e))}


class ClassifyTest(unittest.TestCase):
    def test_groups_and_subgroups(self):
        df = pd.DataFrame([
            app(race=[5]),                 # white
            app(race=[22]),                # Chinese -> api
            app(race=[5, 3]),              # white + black -> black
            app(race=[2, 3]),              # two minority races -> multiple
            app(race=[5], eth=[11]),       # Mexican, white -> hispanic
            app(race=[41]),                # Native Hawaiian -> api
            app(race=[1]),                 # American Indian or Alaska Native -> native
            app(race=[6], eth=[3]),        # not provided -> missing
            app(race=[25, 22]),            # Korean listed first -> Korean
        ])
        g = bh.applicant_group(df)
        self.assertEqual(g.tolist()[:7], ["white", "api", "black", "multiple", "hispanic", "api", "native"])
        self.assertTrue(pd.isna(g.iloc[7]))
        s = bh.applicant_subgroup(df, g)
        self.assertEqual([s.iloc[i] for i in (1, 4, 5, 8)], ["Chinese", "Mexican", "Native Hawaiian", "Korean"])
        self.assertTrue(pd.isna(s.iloc[0]))

    def test_coethnic(self):
        df = pd.DataFrame([app(race=[22]), app(race=[5]), app(race=[6], eth=[3])])
        out = bh.classify(df, pd.Series(["api", "api", "api"]))
        self.assertEqual(out.coethnic.tolist()[:2], [1.0, 0.0])
        self.assertTrue(pd.isna(out.coethnic.iloc[2]))


class MatchLeiTest(unittest.TestCase):
    def test_panel_then_names(self):
        banks = pd.DataFrame({"cert": [1, 2, 3, 4], "name": ["Alpha Bank", "Beta Bank, N.A.", "First Bank", None],
                              "fed_rssd": [111, 222, 333, 444], "ethnic_group": ["Hispanic American"] * 4})
        filers = pd.DataFrame({"lei": ["L1", "L2", "L3a", "L3b", "L4"],
                               "name": ["ALPHA BANK", "BETA BANK", "FIRST BANK", "First Bank", None], "year": 2022})
        panel = pd.DataFrame({"lei": ["L1"], "respondent_rssd": [111]})
        m = bh.match_leis(banks, filers, panel).set_index("lei")
        self.assertEqual(m.loc["L1", "method"], "panel_rssd")
        self.assertEqual(m.loc["L2", ["cert", "method", "accept"]].tolist(), [2, "name", True])
        self.assertFalse(m.loc[["L3a", "L3b"], "accept"].any())      # two LEIs share the name
        self.assertNotIn("L4", m.index)                                # blank names never match
        self.assertEqual(m.loc["L2", "hmda_name"], "BETA BANK")


class SummaryTest(unittest.TestCase):
    def setUp(self):
        df = pd.DataFrame([
            app(race=[22], action=1), app(race=[22], action=3), app(race=[5], action=1),
            app(race=[5], action=3), app(race=[5], action=6),          # purchased loan: not an application
            app(race=[25], action=1, county="06059"),
        ])
        self.apps = bh.classify(df, pd.Series(["api"] * len(df)))

    def test_bank_year(self):
        y = bh.bank_year(self.apps).iloc[0]
        self.assertEqual((y.n_applications, y.n_originations), (5, 3))
        self.assertAlmostEqual(y.denial_rate, 2 / 5)
        self.assertAlmostEqual(y.orig_share_api, 2 / 3)
        self.assertAlmostEqual(y.coethnic_orig_share, 2 / 3)
        self.assertAlmostEqual(y.denial_rate_coethnic, 1 / 3)
        self.assertAlmostEqual(y.denial_rate_other, 1 / 2)
        sub = bh.subgroup_year(self.apps).set_index("applicant_subgroup").n_originations.to_dict()
        self.assertEqual(sub, {"Chinese": 1, "Korean": 1})

    def test_baseline(self):
        lar = pd.DataFrame([app(race=[22]), app(race=[5]), app(race=[5]), app(race=[5]),
                            app(race=[25], county="06059"), app(race=[5], county="06059")])
        base = bh.county_baseline(lar).set_index("county_code")
        self.assertAlmostEqual(base.loc["06037", "county_share_api"], 0.25)
        cmp = bh.baseline_compare(self.apps, base.reset_index(), pd.Series(["api"] * len(self.apps))).iloc[0]
        self.assertAlmostEqual(cmp.coethnic_share_bc, 2 / 3)
        self.assertAlmostEqual(cmp.expected_coethnic_share_bc, (0.25 + 0.25 + 0.5) / 3)


class IoTest(unittest.TestCase):
    def test_read_and_pull(self):
        header = ",".join(["activity_year", "lei", "county_code", "action_taken", "loan_amount", "derived_race",
                           *bh.ETHN_COLS, *bh.RACE_COLS, "aus-1"])
        row = ",".join(["2022", "L1", "06037", "1", "305000", "Asian", "2", "", "", "", "", "22", "", "", "", "", "1"])
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(bh, "RAW", Path(tmp)), \
                mock.patch("builtins.print"):
            def fake_download(url, path):
                self.assertIn("leis=L1,L2", url)
                path.write_text(header + "\n" + row + "\n")
                return path
            with mock.patch.object(bh, "download", side_effect=fake_download):
                lar = bh.pull_lar([2022], "leis", ["L1", "L2"], "banks")
        self.assertEqual(lar.loc[0, "county_code"], "06037")
        self.assertEqual(lar.loc[0, "loan_amount"], 305000)
        self.assertNotIn("aus-1", lar)


if __name__ == "__main__":
    unittest.main()
