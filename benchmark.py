"""同一取引日の調整済み終値による、配当再投資リターンの近似比較。"""
from __future__ import annotations

import math

import pandas as pd

# 1306は2026-03-30/31の二重分割調整を確認。連続性を検証した1305を使用。
BENCHMARK_SYMBOL = "1305.T"
PERIODS = (3, 5, 10)
ROLLING_MONTHS = 36


def clean_history(series):
    if series is None or series.empty:
        return pd.Series(dtype=float, index=pd.DatetimeIndex([]))
    clean = series.copy()
    if clean.index.tz is not None:
        clean.index = clean.index.tz_convert("Asia/Tokyo").tz_localize(None)
    clean.index = clean.index.normalize()
    clean = clean[~clean.index.duplicated(keep="last")].sort_index()
    return clean[clean.map(lambda x: pd.notna(x) and math.isfinite(x) and x > 0)]


def _period_result(aligned, bad_steps, end, years):
    missing = {"available": False, "reason": f"{years}年分の比較可能な履歴がありません"}
    target = end - pd.DateOffset(years=years)
    pos = aligned.index.searchsorted(target, side="right") - 1
    if pos < 0:
        return missing
    start = aligned.index[pos]
    if (target - start).days > 7:
        return missing
    if bad_steps.loc[end] - bad_steps.loc[start] > 0:
        return {"available": False, "reason": "価格履歴に大きな不連続があるため要確認"}
    growth = aligned.loc[end] / aligned.loc[start]
    elapsed_years = (end - start).days / 365.25
    cumulative = (growth - 1) * 100
    annualized = (growth ** (1 / elapsed_years) - 1) * 100
    return {
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


def _rolling_summary(aligned, benchmark, bad_steps, end, years):
    # 最新終点 + 直前35か月の各月末。各回の保有年数を固定する。
    month = end.to_period("M")
    targets = [end] + [(month - i).end_time.normalize() for i in range(1, ROLLING_MONTHS)]
    samples = []
    for target in targets:
        pos = benchmark.index.searchsorted(target, side="right") - 1
        if pos < 0:
            continue
        period_end = benchmark.index[pos]
        if (target - period_end).days > 7 or period_end not in aligned.index:
            continue
        result = _period_result(aligned, bad_steps, period_end, years)
        if result["available"]:
            samples.append({key: result[key] for key in ("start", "end", "excess_cagr_pp")})
    gaps = [s["excess_cagr_pp"] for s in samples]
    wins = sum(g > 1e-9 for g in gaps)
    losses = sum(g < -1e-9 for g in gaps)
    count = len(gaps)
    return {
        "count": count, "requested": ROLLING_MONTHS,
        "complete": count == ROLLING_MONTHS,
        "wins": wins, "losses": losses, "ties": count - wins - losses,
        "win_rate": wins / count * 100 if count else None,
        "median_gap": float(pd.Series(gaps).median()) if count else None,
        "min_gap": min(gaps) if count else None,
        "max_gap": max(gaps) if count else None,
        "samples": samples,
    }


def compare_total_returns(stock, benchmark, *, as_of, relisted_date=None):
    """同じ始点/終点で比較。期間不足を上場来の値で代用しない。"""
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
    ratios = aligned.div(aligned.shift(1))
    bad_steps = ((ratios > 5) | (ratios < 0.2)).any(axis=1).cumsum()
    results = {}
    for years in PERIODS:
        result = _period_result(aligned, bad_steps, end, years)
        result["rolling"] = _rolling_summary(aligned, benchmark, bad_steps, end, years)
        results[str(years)] = result
    return results
