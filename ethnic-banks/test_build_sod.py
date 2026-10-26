"""Offline tests for build_sod.py (no FDIC or Census calls). All records are synthetic."""
import math
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import pandas as pd

import build_sod as bs

BANKS = pd.DataFrame({"cert": [1, 2], "ethnic_group": ["Asian or Pacific Islander American", "Hispanic American"]})


def sod_row(cert, year, uni, county, dep, lat=34.05, lon=-118.24):
    return {"CERT": cert, "YEAR": year, "UNINUMBR": uni, "BRNUM": 0, "NAMEBR": "Main", "ADDRESBR": "1 Main St",
            "CITYBR": "Los Angeles", "STALPBR": "CA", "ZIPBR": 90012, "STCNTYBR": county, "SIMS_LATITUDE": lat,
            "SIMS_LONGITUDE": lon, "DEPSUMBR": dep, "BKMO": 1, "BRSERTYP": 11}


class BranchesTest(unittest.TestCase):
    def test_mapping(self):
        raw = pd.DataFrame([sod_row(1, 2020, 10, 6037, 500), sod_row(1, 2020, 10, 6037, 500),
                            sod_row(1, 2020, 11, "36081", 100, lat=0, lon=0)])
        b = bs.branches(raw)
        self.assertEqual(len(b), 2)                       # duplicate branch-year dropped
        self.assertEqual(b.county_fips.tolist(), ["06037", "36081"])
        self.assertEqual(b.zip.iloc[0], "90012")
        self.assertTrue(pd.isna(b.lat.iloc[1]))           # 0,0 is not a location

    def test_missing_field(self):
        raw = pd.DataFrame([sod_row(1, 2020, 10, 6037, 500)]).drop(columns="SIMS_LATITUDE")
        with mock.patch("sys.stderr"):
            b = bs.branches(raw)
        self.assertTrue(b.lat.isna().all())


class AcsTest(unittest.TestCase):
    def test_shares(self):
        header = ["NAME"] + [v for vs in bs.ACS_VARS.values() for v in vs] + ["state", "county"]
        # total, white, black, native, asian, nhpi, multiple, hispanic
        rows = [header, ["X County", "100", "40", "10", "1", "25", "5", "4", "15", "06", "037"],
                ["Empty County", "0", "0", "0", "0", "0", "0", "0", "0", "02", "999"]]
        s = bs.acs_shares(rows, ["state", "county"]).set_index("geoid")
        self.assertAlmostEqual(s.loc["06037", "share_api"], 0.30)
        self.assertAlmostEqual(s.loc["06037", "share_hispanic"], 0.15)
        self.assertTrue(math.isnan(s.loc["02999", "share_white"]))


class ExposureTest(unittest.TestCase):
    def setUp(self):
        self.br = bs.branches(pd.DataFrame([
            sod_row(1, 2020, 10, 6037, 300), sod_row(1, 2020, 11, 36081, 100),
            sod_row(2, 2020, 20, 12086, 0), sod_row(2, 2020, 21, 6037, 0),   # no deposits: equal weights
            sod_row(2, 2020, 22, 2999, 50),                                    # county with no population
        ]))
        share = {f"share_{g}": 0.0 for g in bs.ETH}
        self.shares = pd.DataFrame([
            {"geoid": "06037", "year": 2020, **share, "share_api": 0.4, "share_hispanic": 0.5},
            {"geoid": "36081", "year": 2020, **share, "share_api": 0.2, "share_hispanic": 0.3},
            {"geoid": "12086", "year": 2020, **share, "share_api": 0.0, "share_hispanic": 0.7},
            {"geoid": "02999", "year": 2020, **{g: float("nan") for g in share}},
        ])
        self.shares["year"] = self.shares.year.astype("Int64")

    def test_weights_and_own_share(self):
        e = bs.exposure(self.br, self.shares, "county_fips", "county", BANKS).set_index("cert")
        self.assertAlmostEqual(e.loc[1, "county_share_api"], (300 * 0.4 + 100 * 0.2) / 400)
        self.assertAlmostEqual(e.loc[1, "own_county_share"], 0.35)
        # bank 2: the populated counties have zero deposits, so they are weighted equally
        self.assertAlmostEqual(e.loc[2, "own_county_share"], (0.7 + 0.5) / 2)
        self.assertEqual(e.loc[2, "county_n_branches_matched"], 2)


class GeocodeCacheTest(unittest.TestCase):
    def test_cache(self):
        pts = pd.DataFrame({"lat": [34.05, 40.7], "lon": [-118.24, -73.9]})
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "geo.csv"
            fake = mock.Mock(side_effect=["06037206300", "36081000100"])
            with mock.patch.object(bs, "geocode_tract", fake), mock.patch("builtins.print"):
                first = bs.tracts_for(pts, path)
                second = bs.tracts_for(pts, path)       # served from the cache
        self.assertEqual(fake.call_count, 2)
        self.assertEqual(sorted(second.tract), ["06037206300", "36081000100"])
        self.assertEqual(len(first), 2)


if __name__ == "__main__":
    unittest.main()
