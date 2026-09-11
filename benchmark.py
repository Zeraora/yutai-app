"""同一取引日の調整済み終値による、配当再投資リターンの近似比較。"""
from __future__ import annotations

import math

import pandas as pd

# 1306は2026-03-30/31の二重分割調整を確認。連続性を検証した1305を使用。
BENCHMARK_SYMBOL = "1305.T"
PERIODS = (3, 5, 10)


def clean_history(series):
    if series is None or series.empty:
        return pd.Series(dtype=float, index=pd.DatetimeIndex([]))
    clean = series.copy()
    if clean.index.tz is not None:
        clean.index = clean.index.tz_convert("Asia/Tokyo").tz_localize(None)
    clean.index = clean.index.normalize()
    clean = clean[~clean.index.duplicated(keep="last")].sort_index()
    return clean[clean.map(lambda x: pd.notna(x) and math.isfinite(x) and x > 0)]


def compare_total_returns(stock, benchmark, *, as_of, relisted_date=None):
    """銘柄・ETFの同じ始点/終点を比較。期間不足を短期の値で代用しない。"""
    stock, benchmark = clean_history(stock), clean_history(benchmark)
    as_of = pd.Timestamp(as_of).normalize()
    benchmark = benchmark.loc[:as_of]
    stock = stock.loc[:as_of]
    if relisted_date:
        stock = stock.loc[relisted_date:]

    def unavailable(reason):
        return {str(years): {"available": False, "reason": reason} for years in PERIODS}

    if benchmark.empty:
        return unavailable("比較対象のデータを取得できません")
    end = benchmark.index[-1]
    if (as_of - end).days > 7:
        return unavailable("比較対象のデータが古いため比較できません")
    if end not in stock.index:
        return unavailable("同じ終点日の株価がありません")
    aligned = pd.concat({"stock": stock, "benchmark": benchmark}, axis=1).dropna()
    results = {}
    for years in PERIODS:
        result = {"available": False, "reason": f"{years}年分の比較可能な履歴がありません"}
        results[str(years)] = result
        target = end - pd.DateOffset(years=years)
        history = aligned.loc[:target]
        if history.empty or (target - history.index[-1]).days > 7:
            continue
        start = history.index[-1]
        window = aligned.loc[start:end]
        # 未調整の分割・単位異常等をリターンとして表示しない。
        ratios = window.div(window.shift(1))
        if ((ratios > 5) | (ratios < 0.2)).any().any():
            result["reason"] = "価格履歴に大きな不連続があるため要確認"
            continue
        growth = aligned.loc[end] / aligned.loc[start]
        elapsed_years = (end - start).days / 365.25
        cumulative = (growth - 1) * 100
        annualized = (growth ** (1 / elapsed_years) - 1) * 100
        results[str(years)] = {
            "available": True,
            "start": start.date().isoformat(),
            "end": end.date().isoformat(),
            "stock_return": float(cumulative["stock"]),
            "benchmark_return": float(cumulative["benchmark"]),
            "excess_pp": float(cumulative["stock"] - cumulative["benchmark"]),
            "stock_cagr": float(annualized["stock"]),
            "benchmark_cagr": float(annualized["benchmark"]),
            "excess_cagr_pp": float(annualized["stock"] - annualized["benchmark"]),
        }
    return results
