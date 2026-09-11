import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import pandas as pd

from benchmark import compare_total_returns


class TotalReturnComparisonTests(unittest.TestCase):
    def setUp(self):
        self.dates = pd.date_range("2015-09-11", "2026-09-11", freq="B")
        years = (self.dates - self.dates[0]).days / 365.25
        self.stock = pd.Series(100 * 1.10 ** years, index=self.dates)
        self.etf = pd.Series(100 * 1.05 ** years, index=self.dates)

    def compare(self, stock=None, etf=None, **kwargs):
        return compare_total_returns(
            self.stock if stock is None else stock,
            self.etf if etf is None else etf,
            as_of=kwargs.pop("as_of", "2026-09-11"), **kwargs,
        )

    def test_cumulative_percentage_points_and_compound_annual_return(self):
        for years, result in self.compare().items():
            self.assertTrue(result["available"])
            elapsed = (pd.Timestamp(result["end"]) - pd.Timestamp(result["start"])).days / 365.25
            expected_stock = (1.1 ** elapsed - 1) * 100
            expected_etf = (1.05 ** elapsed - 1) * 100
            self.assertAlmostEqual(result["stock_return"], expected_stock)
            self.assertAlmostEqual(result["excess_pp"], expected_stock - expected_etf)
            self.assertAlmostEqual(result["stock_cagr"], 10)
            self.assertAlmostEqual(result["benchmark_cagr"], 5)
            self.assertAlmostEqual(result["excess_cagr_pp"], 5)

    def test_missing_start_day_aligns_both_series_to_previous_common_day(self):
        result = self.compare(stock=self.stock.drop(pd.Timestamp("2023-09-11")))["3"]
        self.assertEqual(result["start"], "2023-09-08")
        self.assertAlmostEqual(result["stock_cagr"], 10)
        self.assertAlmostEqual(result["benchmark_cagr"], 5)

    def test_missing_endpoint_is_unknown_not_a_stale_comparison(self):
        results = self.compare(stock=self.stock.iloc[:-1])
        self.assertTrue(all(not r["available"] for r in results.values()))

    def test_recent_listing_does_not_fall_back_to_since_listing(self):
        results = self.compare(stock=self.stock.loc["2022-01-01":])
        self.assertTrue(results["3"]["available"])
        self.assertFalse(results["5"]["available"])
        self.assertFalse(results["10"]["available"])

    def test_relisting_excludes_old_listing_history(self):
        results = self.compare(relisted_date="2025-12-17")
        self.assertTrue(all(not r["available"] for r in results.values()))

    def test_unavailable_and_stale_benchmark_are_unknown(self):
        for etf in (pd.Series(dtype=float), self.etf.loc[:"2026-08-01"]):
            self.assertTrue(all(not r["available"] for r in self.compare(etf=etf).values()))

    def test_split_adjusted_units_do_not_change_returns(self):
        before = self.compare()
        after = self.compare(stock=self.stock / 3, etf=self.etf / 10)
        for years in before:
            self.assertAlmostEqual(before[years]["excess_pp"], after[years]["excess_pp"])

    def test_unadjusted_split_is_not_reported_as_a_loss(self):
        broken = self.etf.copy()
        broken.loc["2026-04-01":] /= 10
        self.assertTrue(all(not r["available"] for r in self.compare(etf=broken).values()))

    def test_timezone_and_leap_day(self):
        stock = self.stock.copy()
        stock.index = stock.index.tz_localize("Asia/Tokyo")
        result = self.compare(stock=stock, as_of="2024-02-29")["3"]
        self.assertEqual(result["start"], "2021-02-26")
        self.assertEqual(result["end"], "2024-02-29")

    def test_invalid_values_cannot_produce_infinity(self):
        stock = self.stock.copy()
        stock.iloc[-1] = float("inf")
        self.assertFalse(self.compare(stock=stock)["3"]["available"])


class ComparisonIntegrationTests(unittest.TestCase):
    def test_api_and_static_generation_include_same_comparison(self):
        import app
        import generator

        stocks = [{"code": "TEST", "name": "Test", "starred": True}]
        comparisons = {"TEST": {"3": {"available": True, "stock_return": 80, "benchmark_return": 50, "excess_pp": 30}}}
        metrics = tuple({"TEST": 100.0} for _ in range(15)) + (comparisons,)
        with patch.object(app, "STOCKS", stocks), patch.object(app, "fetch_prices_and_rsi", return_value=metrics), patch.object(app, "fetch_metrics", return_value={}):
            response = app.app.test_client().get("/api/prices")
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.json["topix_comparisons"], comparisons)
        with tempfile.TemporaryDirectory() as temp, patch.object(generator, "__file__", str(Path(temp) / "generator.py")), patch.object(generator, "load_stocks", return_value=stocks), patch.object(generator, "fetch_prices_and_rsi", return_value=metrics), patch.object(generator, "fetch_dividend_yields", return_value={}), patch.object(app, "STOCKS", stocks):
            generator.main()
            html = (Path(temp) / "docs/index.html").read_text()
            embedded = html.split("const EMBEDDED_API_DATA = ", 1)[1].split(";\n", 1)[0]
            self.assertEqual(json.loads(embedded)["topix_comparisons"], comparisons)
            self.assertIn('id="tab-topix"', html)
            self.assertNotIn("{{ stocks_json | safe }}", html)

    def test_generator_retains_previous_page_when_benchmark_unavailable(self):
        import app
        import generator

        stocks = [{"code": "TEST", "starred": True}]
        metrics = tuple({"TEST": 100.0} for _ in range(15)) + ({"TEST": {"3": {"available": False}}},)
        with tempfile.TemporaryDirectory() as temp, patch.object(generator, "__file__", str(Path(temp) / "generator.py")), patch.object(generator, "load_stocks", return_value=stocks), patch.object(generator, "fetch_prices_and_rsi", return_value=metrics), patch.object(generator, "fetch_dividend_yields", return_value={}), patch.object(app, "STOCKS", stocks):
            target = Path(temp) / "docs/index.html"
            target.parent.mkdir()
            target.write_text("previous good page")
            with self.assertRaises(RuntimeError):
                generator.main()
            self.assertEqual(target.read_text(), "previous good page")


if __name__ == "__main__":
    unittest.main()
