"""
自社製品・自社事業の株主優待を実施している東証プライム銘柄一覧 Web App.

使い方:
    pip install -r requirements.txt
    python app.py
    → ブラウザで http://localhost:5001 を開く
    → 「価格を更新」ボタンで最新株価を取得して必要額を再計算
"""

from __future__ import annotations

import json
import math
import os
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import yfinance as yf
from flask import Flask, Response, jsonify, redirect, render_template_string, request

app = Flask(__name__)
JST = ZoneInfo("Asia/Tokyo")
# 上場廃止を挟んだ銘柄は、旧上場時代と連続比較せず再上場日から扱う。
RELISTED_DATES = {"8303": "2025-12-17"}

# === Basic 認証 (環境変数で有効化) ===
# BASIC_AUTH_USER と BASIC_AUTH_PASS が両方設定されているときのみ認証を要求。
# ローカル開発時は環境変数なしで動作(認証なし)。
# クラウドデプロイ時に PythonAnywhere の Web 設定で環境変数を設定すれば有効化。
_AUTH_USER = os.environ.get("BASIC_AUTH_USER")
_AUTH_PASS = os.environ.get("BASIC_AUTH_PASS")


@app.before_request
def _require_basic_auth():
    if not _AUTH_USER or not _AUTH_PASS:
        return  # 環境変数未設定なら認証なし(ローカル開発)
    a = request.authorization
    if not a or a.username != _AUTH_USER or a.password != _AUTH_PASS:
        return Response(
            "認証が必要です", 401,
            {"WWW-Authenticate": 'Basic realm="watchlist"'},
        )

STOCKS_FILE = Path(__file__).parent / "stocks.json"
_stocks_lock = threading.Lock()


def load_stocks() -> list[dict[str, Any]]:
    with STOCKS_FILE.open(encoding="utf-8") as f:
        return json.load(f)


def save_stocks(stocks: list[dict[str, Any]]) -> None:
    """一時ファイル経由でアトミックに上書き保存。"""
    tmp = STOCKS_FILE.with_suffix(".tmp.json")
    with tmp.open("w", encoding="utf-8") as f:
        json.dump(stocks, f, ensure_ascii=False, indent=2)
    tmp.replace(STOCKS_FILE)


STOCKS: list[dict[str, Any]] = load_stocks()


def _compute_rsi_weekly_values(
    close, period: int = 14, sma_period: int = 14,
) -> tuple[float | None, float | None]:
    """週足Wilder RSI(14)と、そのRSIの14週SMAを返す。"""
    if close is None or len(close) < 5:
        return None, None
    # 各週の最終取引日の終値を週足とする
    weekly = close.resample("W").last().dropna()
    if len(weekly) < period + 1:
        return None, None
    delta = weekly.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    avg_gain = gain.ewm(alpha=1 / period, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1 / period, adjust=False).mean()
    rs = avg_gain / avg_loss.where(avg_loss != 0)
    rsi_series = 100 - (100 / (1 + rs))
    rsi_series = rsi_series.where(avg_loss != 0, 100.0).dropna()
    if len(rsi_series) == 0:
        return None, None
    current = float(rsi_series.iloc[-1])
    rsi_sma = (
        float(rsi_series.iloc[-sma_period:].mean())
        if len(rsi_series) >= sma_period else None
    )
    return current, rsi_sma


def _compute_rsi_weekly(close, period: int = 14) -> float | None:
    """互換用: 日足 Close から最新の週足Wilder RSIだけを返す。"""
    current, _sma = _compute_rsi_weekly_values(close, period=period)
    return current


def _compute_bollinger(close, period: int = 20, std_mult: float = 2.0) -> tuple[float | None, float | None]:
    """20日±2σ のボリンジャーバンド上下限を返す。"""
    if close is None or len(close) < period:
        return None, None
    window = close.iloc[-period:]
    sma = float(window.mean())
    std = float(window.std())
    if math.isnan(sma) or math.isnan(std):
        return None, None
    return sma + std_mult * std, sma - std_mult * std


def _remove_extreme_price_outliers(code: str, close):
    """中央値から極端に離れた単発の価格異常値を除外する。"""
    if close is None or len(close) == 0:
        return close
    clean = close.dropna()
    if len(clean) < 5:
        return clean
    median = float(clean.median())
    if not math.isfinite(median) or median <= 0:
        return clean

    # 取得期間内の中央値から20倍超の乖離はデータ異常とみなす。
    filtered = clean[(clean >= median / 20) & (clean <= median * 20)]
    removed = len(clean) - len(filtered)
    # 正常データを誤って大きく削らないよう、80%以上残る場合だけ採用する。
    if removed > 0 and len(filtered) >= max(5, math.ceil(len(clean) * 0.8)):
        app.logger.warning(
            "removed %d extreme price outlier(s) for %s", removed, code,
        )
        return filtered
    return clean


def _compute_three_year_comparison(close) -> tuple[float | None, float | None]:
    """現在値と3年前時点までの20営業日平均から (基準株価, 騰落率%) を返す。"""
    if close is None or len(close) < 20:
        return None, None
    clean = close.dropna().sort_index()
    if len(clean) < 20:
        return None, None
    latest_date = clean.index[-1]
    try:
        target_date = latest_date.replace(year=latest_date.year - 3)
    except ValueError:  # うるう日の場合は2月28日を使う
        target_date = latest_date.replace(year=latest_date.year - 3, day=28)
    baseline_window = clean.loc[:target_date].tail(20)
    if len(baseline_window) < 20:
        return None, None
    # 基準期間内に明らかな単発異常値があれば除外して平均を守る。
    window_median = float(baseline_window.median())
    if not math.isfinite(window_median) or window_median <= 0:
        return None, None
    baseline_window = baseline_window[
        (baseline_window >= window_median / 5)
        & (baseline_window <= window_median * 5)
    ]
    if len(baseline_window) < 15:
        return None, None
    baseline = float(baseline_window.mean())
    current = float(clean.iloc[-1])
    if not math.isfinite(baseline) or not math.isfinite(current) or baseline <= 0:
        return None, None
    change_pct = (current / baseline - 1) * 100
    return baseline, change_pct


def _compute_long_term_comparison(
    code: str, close,
) -> tuple[float | None, float | None, str | None, float | None]:
    """10年前比。履歴10年未満は上場来、再上場銘柄は再上場来で比較する。"""
    if close is None or len(close) < 20:
        return None, None, None, None
    clean = close.dropna().sort_index()
    if len(clean) < 20:
        return None, None, None, None

    latest_date = clean.index[-1]
    relisted_date = RELISTED_DATES.get(code)
    if relisted_date:
        # Yahooに混入する再上場直後の巨大異常値を、再上場後全体の中央値で除く。
        segment = clean.loc[relisted_date:]
        if len(segment) < 20:
            return None, None, None, None
        segment_median = float(segment.median())
        if not math.isfinite(segment_median) or segment_median <= 0:
            return None, None, None, None
        segment = segment[
            (segment >= segment_median / 5)
            & (segment <= segment_median * 5)
        ]
        if len(segment) < 20:
            return None, None, None, None
        baseline_window = segment.head(20)
        label = "再上場来"
        years = (segment.index[-1] - segment.index[0]).days / 365.25
    else:
        try:
            target_date = latest_date.replace(year=latest_date.year - 10)
        except ValueError:
            target_date = latest_date.replace(year=latest_date.year - 10, day=28)
        baseline_window = clean.loc[:target_date].tail(20)
        if len(baseline_window) == 20:
            label = "10年前比"
            years = 10.0
        else:
            baseline_window = clean.head(20)
            label = "上場来"
            years = (latest_date - clean.index[0]).days / 365.25

    window_median = float(baseline_window.median())
    if not math.isfinite(window_median) or window_median <= 0:
        return None, None, None, None
    baseline_window = baseline_window[
        (baseline_window >= window_median / 5)
        & (baseline_window <= window_median * 5)
    ]
    if len(baseline_window) < 15:
        return None, None, None, None
    baseline = float(baseline_window.mean())
    current = float(clean.iloc[-1])
    if not math.isfinite(baseline) or not math.isfinite(current) or baseline <= 0:
        return None, None, None, None
    change_pct = (current / baseline - 1) * 100
    return baseline, change_pct, label, years


def _compute_buy_targets(close) -> tuple[float | None, float | None]:
    """
    日足 Close から (200日SMA, RSI30到達価格) を返す。
    - SMA200: 過去200日の単純移動平均
    - RSI30到達価格: 次週その価格まで下落すれば週足RSIが30に到達(1週間想定の逆算)
      数式: P = last_w_close - (1-α)(7g - 3l) / (3α)  (α=1/14)
    """
    sma200: float | None = None
    rsi30: float | None = None
    if close is None or len(close) == 0:
        return sma200, rsi30
    # SMA200 (日足ベース)
    if len(close) >= 200:
        sma200 = float(close.iloc[-200:].mean())
    # RSI30到達価格 (週足ベース)
    weekly = close.resample("W").last().dropna()
    period = 14
    if len(weekly) >= period + 1:
        alpha = 1 / period
        delta = weekly.diff()
        gain = delta.clip(lower=0)
        loss = -delta.clip(upper=0)
        avg_gain = gain.ewm(alpha=alpha, adjust=False).mean()
        avg_loss = loss.ewm(alpha=alpha, adjust=False).mean()
        try:
            g = float(avg_gain.iloc[-1])
            l = float(avg_loss.iloc[-1])
            last_w_close = float(weekly.iloc[-1])
            if 7 * g > 3 * l:  # 現在RSIが30より上 → 下落想定で計算
                decline = (1 - alpha) * (7 * g - 3 * l) / (3 * alpha)
                p = last_w_close - decline
                if p > 0 and not math.isnan(p):
                    rsi30 = float(p)
        except (ValueError, ZeroDivisionError, OverflowError):
            pass
    return sma200, rsi30


def fetch_prices_and_rsi() -> tuple[
    dict[str, float | None],
    dict[str, float | None],
    dict[str, float | None],
    dict[str, float | None],
    dict[str, float | None],
    dict[str, float | None],
    dict[str, float | None],
    dict[str, float | None],
    dict[str, float | None],
    dict[str, float | None],
    dict[str, float | None],
    dict[str, float | None],
    dict[str, float | None],
    dict[str, str | None],
    dict[str, float | None],
]:
    """価格 / RSI / 52週高安 / BB / 3年前比較などを一括取得。"""
    symbols = [f"{s['code']}.T" for s in STOCKS]
    prices: dict[str, float | None] = {s["code"]: None for s in STOCKS}
    rsis: dict[str, float | None] = {s["code"]: None for s in STOCKS}
    rsi_sma14s: dict[str, float | None] = {s["code"]: None for s in STOCKS}
    sma200s: dict[str, float | None] = {s["code"]: None for s in STOCKS}
    rsi30s: dict[str, float | None] = {s["code"]: None for s in STOCKS}
    high52s: dict[str, float | None] = {s["code"]: None for s in STOCKS}
    low52s: dict[str, float | None] = {s["code"]: None for s in STOCKS}
    bb_uppers: dict[str, float | None] = {s["code"]: None for s in STOCKS}
    bb_lowers: dict[str, float | None] = {s["code"]: None for s in STOCKS}
    price3y_refs: dict[str, float | None] = {s["code"]: None for s in STOCKS}
    price3y_changes: dict[str, float | None] = {s["code"]: None for s in STOCKS}
    long_refs: dict[str, float | None] = {s["code"]: None for s in STOCKS}
    long_changes: dict[str, float | None] = {s["code"]: None for s in STOCKS}
    long_labels: dict[str, str | None] = {s["code"]: None for s in STOCKS}
    long_years: dict[str, float | None] = {s["code"]: None for s in STOCKS}

    def _populate(code: str, close, price_only_close=None):
        if close is None or len(close) == 0: return
        # 既存3指標は従来どおり直近約1年だけで計算する。
        # 長期履歴全体を混ぜると、古い異常値やRSI初期値が結果を変えるため。
        close = _remove_extreme_price_outliers(code, close.tail(260))
        if len(close) == 0: return
        prices[code] = float(close.iloc[-1])
        rsis[code], rsi_sma14s[code] = _compute_rsi_weekly_values(close)
        sma, r30 = _compute_buy_targets(close)
        sma200s[code] = sma
        rsi30s[code] = r30
        bbu, bbl = _compute_bollinger(close)
        bb_uppers[code] = bbu
        bb_lowers[code] = bbl
        n = min(252, len(close))
        if n > 0:
            high52s[code] = float(close.iloc[-n:].max())
            low52s[code] = float(close.iloc[-n:].min())
        comparison_close = price_only_close if price_only_close is not None else close
        if code not in RELISTED_DATES:
            price3y_refs[code], price3y_changes[code] = _compute_three_year_comparison(comparison_close)
        (
            long_refs[code], long_changes[code],
            long_labels[code], long_years[code],
        ) = _compute_long_term_comparison(code, comparison_close)

    today = datetime.now(JST).date()
    try:
        history_start = today.replace(year=today.year - 11).isoformat()
    except ValueError:
        history_start = today.replace(year=today.year - 11, day=28).isoformat()
    try:
        data = yf.download(
            symbols, start=history_start, progress=False,
            group_by="ticker", auto_adjust=False, threads=True,
        )
    except Exception as e:
        app.logger.warning(f"bulk download failed: {e}")
        data = None

    if data is not None:
        for s in STOCKS:
            sym = f"{s['code']}.T"
            try:
                price_only_close = data[sym]["Close"].dropna()
                adjusted_close = data[sym]["Adj Close"].dropna()
                _populate(s["code"], adjusted_close, price_only_close)
            except (KeyError, ValueError, AttributeError):
                pass

    missing = [code for code, price in prices.items() if price is None]
    for code in missing:
        try:
            t = yf.Ticker(f"{code}.T")
            hist = t.history(start=history_start, auto_adjust=False)
            if not hist.empty:
                _populate(code, hist["Adj Close"], hist["Close"])
        except Exception as e:
            app.logger.warning(f"individual fetch failed for {code}: {e}")

    return (
        prices, rsis, rsi_sma14s, sma200s, rsi30s,
        high52s, low52s, bb_uppers, bb_lowers,
        price3y_refs, price3y_changes,
        long_refs, long_changes, long_labels, long_years,
    )


def _compute_avg_roe(ticker) -> float | None:
    """直近年次の純利益と純資産から年次ROE = NI/Equity の平均を計算。"""
    try:
        income = ticker.income_stmt
        balance = ticker.balance_sheet
        if income is None or balance is None or income.empty or balance.empty:
            return None
        ni_row = None
        for key in ("Net Income Common Stockholders", "Net Income"):
            if key in income.index:
                ni_row = income.loc[key]
                break
        eq_row = None
        for key in ("Stockholders Equity", "Common Stock Equity", "Total Equity Gross Minority Interest"):
            if key in balance.index:
                eq_row = balance.loc[key]
                break
        if ni_row is None or eq_row is None:
            return None
        roes: list[float] = []
        for date in ni_row.index:
            if date not in eq_row.index:
                continue
            try:
                ni = float(ni_row[date])
                eq = float(eq_row[date])
            except (ValueError, TypeError):
                continue
            if math.isnan(ni) or math.isnan(eq) or eq == 0:
                continue
            roes.append(ni / eq)
        if not roes:
            return None
        return float(sum(roes) / len(roes))
    except Exception:
        return None


def _compute_equity_ratio(ticker) -> float | None:
    """自己資本比率 = 純資産 / 総資産 (直近年度)。"""
    try:
        balance = ticker.balance_sheet
        if balance is None or balance.empty:
            return None
        eq_row = None
        for key in ("Stockholders Equity", "Common Stock Equity", "Total Equity Gross Minority Interest"):
            if key in balance.index:
                eq_row = balance.loc[key]
                break
        ta_row = balance.loc["Total Assets"] if "Total Assets" in balance.index else None
        if eq_row is None or ta_row is None or len(ta_row) == 0:
            return None
        # 直近(最新)カラム
        latest = ta_row.index[0]
        try:
            eq = float(eq_row[latest])
            ta = float(ta_row[latest])
        except (KeyError, ValueError, TypeError):
            return None
        if ta <= 0 or math.isnan(eq) or math.isnan(ta):
            return None
        return eq / ta
    except Exception:
        return None


def _fetch_metrics(code: str) -> tuple[str, dict[str, float | None]]:
    """単一銘柄の各種指標を取得(B案11指標化対応 + 自己資本比率)。"""
    try:
        t = yf.Ticker(f"{code}.T")
        info = t.info
        roe = info.get("returnOnEquity")
        per = info.get("trailingPE") or info.get("forwardPE")
        pbr = info.get("priceToBook")
        div_yield = info.get("dividendYield")
        peg = info.get("pegRatio")
        earnings_growth = info.get("earningsGrowth")
        payout_ratio = info.get("payoutRatio")
        avg_roe = _compute_avg_roe(t)
        equity_ratio = _compute_equity_ratio(t)
        return code, {
            "roe": float(roe) if roe is not None else None,
            "per": float(per) if per is not None else None,
            "avg_roe": avg_roe,
            "pbr": float(pbr) if pbr is not None else None,
            "div_yield": float(div_yield) if div_yield is not None else None,
            "peg": float(peg) if peg is not None else None,
            "earnings_growth": float(earnings_growth) if earnings_growth is not None else None,
            "payout_ratio": float(payout_ratio) if payout_ratio is not None else None,
            "equity_ratio": equity_ratio,
        }
    except Exception:
        return code, {"roe": None, "per": None, "avg_roe": None, "pbr": None,
                      "div_yield": None, "peg": None,
                      "earnings_growth": None, "payout_ratio": None,
                      "equity_ratio": None}


def fetch_metrics() -> dict[str, dict[str, float | None]]:
    """全銘柄の ROE / PER / 過去ROE平均 を並列取得。"""
    codes = [s["code"] for s in STOCKS]
    with ThreadPoolExecutor(max_workers=10) as ex:
        return dict(ex.map(_fetch_metrics, codes))


def _fetch_dividend_yield(code: str) -> tuple[str, float | None]:
    """参考表示用の配当利回りだけを取得する。判定・並び順には使用しない。"""
    try:
        value = yf.Ticker(f"{code}.T").info.get("dividendYield")
        return code, float(value) if value is not None else None
    except Exception:
        return code, None


def fetch_dividend_yields() -> dict[str, float | None]:
    """全銘柄の配当利回りを並列取得する。"""
    codes = [s["code"] for s in STOCKS]
    with ThreadPoolExecutor(max_workers=10) as ex:
        return dict(ex.map(_fetch_dividend_yield, codes))


@app.route("/api/prices")
def api_prices():
    (
        prices, rsis, rsi_sma14s, sma200s, rsi30s,
        high52s, low52s, bb_uppers, bb_lowers,
        price3y_refs, price3y_changes,
        long_refs, long_changes, long_labels, long_years,
    ) = fetch_prices_and_rsi()
    metrics = fetch_metrics()
    roes = {code: m["roe"] for code, m in metrics.items()}
    pers = {code: m["per"] for code, m in metrics.items()}
    avg_roes = {code: m["avg_roe"] for code, m in metrics.items()}
    pbrs = {code: m["pbr"] for code, m in metrics.items()}
    div_yields = {code: m["div_yield"] for code, m in metrics.items()}
    pegs = {code: m["peg"] for code, m in metrics.items()}
    earnings_growths = {code: m["earnings_growth"] for code, m in metrics.items()}
    payout_ratios = {code: m["payout_ratio"] for code, m in metrics.items()}
    equity_ratios = {code: m["equity_ratio"] for code, m in metrics.items()}
    verified = {s["code"]: s.get("last_verified") for s in STOCKS}
    return jsonify({
        "prices": prices,
        "roes": roes,
        "pers": pers,
        "avg_roes": avg_roes,
        "rsis": rsis,
        "rsi_sma14s": rsi_sma14s,
        "sma200s": sma200s,
        "rsi30_prices": rsi30s,
        "high52s": high52s,
        "low52s": low52s,
        "bb_uppers": bb_uppers,
        "bb_lowers": bb_lowers,
        "price3y_refs": price3y_refs,
        "price3y_changes": price3y_changes,
        "long_refs": long_refs,
        "long_changes": long_changes,
        "long_labels": long_labels,
        "long_years": long_years,
        "pbrs": pbrs,
        "div_yields": div_yields,
        "pegs": pegs,
        "earnings_growths": earnings_growths,
        "payout_ratios": payout_ratios,
        "equity_ratios": equity_ratios,
        "verified": verified,
        "fetched_at": datetime.now(JST).strftime("%Y-%m-%d %H:%M:%S JST"),
    })


@app.route("/api/verify/<code>", methods=["POST"])
def api_verify(code: str):
    today = datetime.now(JST).strftime("%Y-%m-%d")
    with _stocks_lock:
        found = False
        for s in STOCKS:
            if s["code"] == code:
                s["last_verified"] = today
                found = True
                break
        if not found:
            return jsonify({"error": f"unknown code: {code}"}), 404
        save_stocks(STOCKS)
    return jsonify({"code": code, "last_verified": today})


@app.route("/api/star/<code>", methods=["POST"])
def api_star(code: str):
    """注目銘柄(starred)をトグルして JSON に永続化。"""
    with _stocks_lock:
        found = None
        for s in STOCKS:
            if s["code"] == code:
                s["starred"] = not s.get("starred", False)
                found = s["starred"]
                break
        if found is None:
            return jsonify({"error": f"unknown code: {code}"}), 404
        save_stocks(STOCKS)
    return jsonify({"code": code, "starred": found})


def _to_optional_float(v):
    if v is None or v == "":
        return None
    try:
        return float(v)
    except (ValueError, TypeError):
        return False  # invalid sentinel


def _to_optional_str(v):
    if v is None:
        return None
    if not isinstance(v, str):
        return False
    s = v.strip()
    return s if s else None


@app.route("/api/analysis/<code>", methods=["POST"])
def api_analysis(code: str):
    """プロ分析 + 優待詳細(株数/円/内容)を保存。"""
    body = request.get_json(silent=True) or {}
    judgment = body.get("judgment")
    target_price = body.get("target_price")
    analysis = body.get("analysis")
    if judgment not in (None, "", "buy", "hold", "avoid"):
        return jsonify({"error": "invalid judgment"}), 400
    if judgment == "":
        judgment = None
    target_price = _to_optional_float(target_price)
    if target_price is False:
        return jsonify({"error": "invalid target_price"}), 400
    analysis = _to_optional_str(analysis)
    if analysis is False:
        return jsonify({"error": "invalid analysis"}), 400
    yutai_shares = _to_optional_float(body.get("yutai_shares"))
    if yutai_shares is False:
        return jsonify({"error": "invalid yutai_shares"}), 400
    yutai_value = _to_optional_float(body.get("yutai_value"))
    if yutai_value is False:
        return jsonify({"error": "invalid yutai_value"}), 400
    yutai_item = _to_optional_str(body.get("yutai_item"))
    if yutai_item is False:
        return jsonify({"error": "invalid yutai_item"}), 400
    with _stocks_lock:
        found = False
        for s in STOCKS:
            if s["code"] == code:
                s["judgment"] = judgment
                s["target_price"] = target_price
                s["analysis"] = analysis
                s["yutai_shares"] = yutai_shares
                s["yutai_value"] = yutai_value
                s["yutai_item"] = yutai_item
                found = True
                break
        if not found:
            return jsonify({"error": f"unknown code: {code}"}), 404
        save_stocks(STOCKS)
    return jsonify({
        "code": code,
        "judgment": judgment,
        "target_price": target_price,
        "analysis": analysis,
        "yutai_shares": yutai_shares,
        "yutai_value": yutai_value,
        "yutai_item": yutai_item,
    })


@app.route("/api/rating/<code>", methods=["POST"])
def api_rating(code: str):
    """優待の魅力評価(good / bad / null)を保存。"""
    body = request.get_json(silent=True) or {}
    new_rating = body.get("rating")
    if new_rating not in (None, "good", "bad"):
        return jsonify({"error": "invalid rating; must be good/bad/null"}), 400
    with _stocks_lock:
        found = False
        for s in STOCKS:
            if s["code"] == code:
                s["rating"] = new_rating
                found = True
                break
        if not found:
            return jsonify({"error": f"unknown code: {code}"}), 404
        save_stocks(STOCKS)
    return jsonify({"code": code, "rating": new_rating})


@app.route("/")
def index():
    return render_template_string(
        INDEX_HTML,
        stocks_json=json.dumps(STOCKS, ensure_ascii=False),
    )


@app.route("/watchlist")
def watchlist():
    starred = [s for s in STOCKS if s.get("starred")]
    return render_template_string(
        WATCHLIST_HTML,
        stocks_json=json.dumps(starred, ensure_ascii=False),
    )


INDEX_HTML = r"""<!DOCTYPE html>
<html lang="ja">
<head>
<meta charset="UTF-8">
<title>自社製品・自社事業の株主優待 一覧(プライム+スタンダード)</title>
<style>
  body {
    font-family: -apple-system, "Hiragino Kaku Gothic ProN", "Yu Gothic", sans-serif;
    max-width: 1400px;
    margin: 1.5em auto;
    padding: 0 1em;
    line-height: 1.6;
    color: #222;
    background: #fafafa;
  }
  h1 {
    border-bottom: 3px solid #c0392b;
    padding-bottom: 0.3em;
    font-size: 1.4em;
  }
  h2 {
    background: #c0392b;
    color: #fff;
    padding: 0.4em 0.8em;
    border-radius: 4px;
    margin-top: 1.8em;
    font-size: 1.05em;
  }
  table {
    width: 100%;
    border-collapse: collapse;
    background: #fff;
    box-shadow: 0 1px 3px rgba(0,0,0,0.08);
    font-size: 0.9em;
  }
  th, td {
    border: 1px solid #ddd;
    padding: 6px 10px;
    text-align: left;
    vertical-align: top;
  }
  thead th {
    background: #f4ebe9;
    white-space: nowrap;
    cursor: pointer;
    user-select: none;
    position: relative;
  }
  thead th:hover { background: #ecd9d4; }
  thead th .arrow { color: #c0392b; font-weight: bold; margin-left: 3px; }
  td.code { font-family: "SF Mono", Menlo, monospace; color: #555; text-align: center; white-space: nowrap; }
  td.name { font-weight: bold; white-space: nowrap; }
  td.num { text-align: right; font-family: "SF Mono", Menlo, monospace; white-space: nowrap; }
  td.loading { color: #aaa; }
  td.error { color: #c0392b; }
  td.roe.high { color: #1e8449; font-weight: bold; }
  td.roe.low { color: #999; }
  td.roe-avg.high { color: #1e8449; font-weight: bold; }
  td.roe-avg.low { color: #999; }
  td.roe.below-avg { background: #fdebd0; }
  td.roe.above-avg { background: #d4efdf; }
  td.rsi.overbought { color: #c0392b; font-weight: bold; }
  td.rsi.oversold { color: #1e8449; font-weight: bold; }
  td.per.cheap { color: #1e8449; font-weight: bold; }
  td.per.expensive { color: #c0392b; }
  td.verified.stale { color: #d35400; background: #fff3e0; font-weight: bold; }
  td.verified.fresh { color: #1e8449; }
  .star-btn {
    background: none;
    border: none;
    cursor: pointer;
    font-size: 1.15em;
    padding: 0 4px;
    color: #ccc;
    line-height: 1;
    vertical-align: middle;
  }
  .star-btn.on { color: #f1c40f; text-shadow: 0 0 2px rgba(241,196,15,0.6); }
  .star-btn:hover { transform: scale(1.2); }
  tbody tr.starred { background: #fffce6; }
  tbody tr.starred:hover { background: #fff8c8; }
  td.rating-cell { white-space: nowrap; text-align: center; }
  .rating-btn {
    background: #ecf0f1;
    border: 1px solid #ccc;
    padding: 1px 8px;
    margin-right: 2px;
    border-radius: 3px;
    cursor: pointer;
    font-size: 0.95em;
    color: #555;
  }
  .rating-btn.good.on { background: #27ae60; color: #fff; border-color: #229954; font-weight: bold; }
  .rating-btn.bad.on  { background: #c0392b; color: #fff; border-color: #a93226; font-weight: bold; }
  .rating-btn:hover { background: #d5dbdb; }
  .rating-btn.good.on:hover { background: #229954; }
  .rating-btn.bad.on:hover  { background: #a93226; }
  tbody tr.rating-bad { opacity: 0.45; }
  tbody tr.rating-bad:hover { opacity: 1; }
  tbody tr.rating-good td.name { color: #196f3d; }
  td.judgment { text-align: center; white-space: nowrap; }
  /* (買い目安関連CSSは廃止、減点法スコアに一本化) */
  td.judgment .jbadge {
    display: inline-block; padding: 2px 8px; border-radius: 3px;
    font-size: 0.85em; font-weight: bold; color: #fff;
  }
  .jbadge.buy   { background: #27ae60; }
  .jbadge.hold  { background: #f39c12; }
  .jbadge.avoid { background: #c0392b; }
  .jbadge.none  { background: #ecf0f1; color: #888; font-weight: normal; }
  td.analysis-cell { max-width: 280px; font-size: 0.85em; line-height: 1.35; }
  td.analysis-cell .edit-link {
    float: right; margin-left: 4px; cursor: pointer; color: #2980b9;
    font-size: 0.85em; text-decoration: underline;
  }
  td.analysis-cell .edit-link:hover { color: #1a5276; }
  /* モーダル */
  .modal-bg {
    display: none; position: fixed; top: 0; left: 0; right: 0; bottom: 0;
    background: rgba(0,0,0,0.5); z-index: 1000;
    align-items: center; justify-content: center;
  }
  .modal-bg.show { display: flex; }
  .modal {
    background: #fff; padding: 1.5em; border-radius: 8px;
    width: 480px; max-width: 90%;
    box-shadow: 0 10px 30px rgba(0,0,0,0.3);
  }
  .modal h3 { margin: 0 0 0.8em; color: #c0392b; }
  .modal label { display: block; margin: 0.6em 0 0.2em; font-weight: bold; font-size: 0.9em; }
  .modal input, .modal select, .modal textarea {
    width: 100%; padding: 0.4em; border: 1px solid #bbb; border-radius: 3px;
    font-size: 0.95em; box-sizing: border-box;
  }
  .modal textarea { resize: vertical; font-family: inherit; min-height: 70px; }
  .modal-actions { margin-top: 1em; text-align: right; }
  .modal-actions button {
    padding: 0.5em 1em; margin-left: 0.5em; border: none;
    border-radius: 4px; cursor: pointer; font-size: 0.9em;
  }
  .modal-actions .btn-save { background: #27ae60; color: #fff; }
  .modal-actions .btn-cancel { background: #95a5a6; color: #fff; }
  td.verify { white-space: nowrap; }
  td.verify a {
    display: inline-block;
    padding: 2px 6px;
    margin-right: 3px;
    background: #ecf0f1;
    color: #2c3e50;
    text-decoration: none;
    border-radius: 3px;
    font-size: 0.85em;
  }
  td.verify a:hover { background: #d5dbdb; }
  td.verify button.verify-btn {
    background: #27ae60;
    color: #fff;
    border: none;
    padding: 2px 8px;
    border-radius: 3px;
    cursor: pointer;
    font-size: 0.85em;
    margin-left: 3px;
  }
  td.verify button.verify-btn:hover { background: #229954; }
  td.verify button.verify-btn:disabled { background: #aaa; cursor: not-allowed; }
  .badge {
    display: inline-block;
    padding: 1px 8px;
    border-radius: 3px;
    font-size: 0.78em;
    font-weight: bold;
    color: #fff;
  }
  .badge-prime { background: #2e86c1; }
  .badge-standard { background: #95a5a6; }
  .vbadge {
    display: inline-block; padding: 2px 9px; border-radius: 3px;
    font-size: 0.92em; font-weight: bold; color: #fff;
  }
  .vbadge.cheap { background: #27ae60; }
  .vbadge.neutral { background: #95a5a6; }
  .vbadge.expensive { background: #c0392b; }
  .vbadge.long-slump { background: #34495e; }
  .premium { font-size: 1.15em; vertical-align: middle; cursor: help; }
  .yield-block {
    background: #f5f7fa; padding: 0.5em 0.7em; border-radius: 4px;
    border-left: 3px solid #95a5a6; line-height: 1.45;
  }
  .yield-block.high { background: #d5f5e3; border-left-color: #27ae60; }
  .yield-block.mid { background: #fcf3cf; border-left-color: #f1c40f; }
  .yield-block.placeholder { font-size: 0.83em; color: #999; font-style: italic; border-left-color: #ddd; }
  .yield-block .ty-num { font-size: 1.15em; }
  .yield-block.high .ty-num { color: #196f3d; }
  .yield-block.mid .ty-num { color: #b7790a; }
  .high-down-med { color: #d35400; font-weight: bold; }
  .high-down-large { color: #c0392b; font-weight: bold; background: #fdebd0; padding: 1px 6px; border-radius: 3px; }
  .ic-grid {
    display: grid;
    grid-template-columns: repeat(4, 1fr);
    gap: 4px;
    margin: 0.4em 0;
  }
  .ic {
    padding: 5px 4px; border-radius: 4px; text-align: center;
    line-height: 1.15; cursor: help;
  }
  .ic-name { font-size: 0.72em; font-weight: bold; opacity: 0.85; }
  .ic-val { font-family: "SF Mono", Menlo, monospace; font-size: 0.85em; margin-top: 1px; }
  .ic-pts { font-size: 0.7em; opacity: 0.7; margin-top: 1px; }
  .ic-good-strong { background: #27ae60; color: #fff; }
  .ic-good        { background: #abebc6; color: #196f3d; }
  .ic-neutral     { background: #ecf0f1; color: #555; }
  .ic-bad         { background: #fadbd8; color: #922b21; }
  .ic-bad-strong  { background: #c0392b; color: #fff; }
  .controls {
    position: sticky;
    top: 0;
    background: #fafafa;
    padding: 0.6em 0;
    z-index: 10;
    border-bottom: 1px solid #eee;
    display: flex;
    align-items: center;
    gap: 1em;
    flex-wrap: wrap;
  }
  button#refresh {
    background: #c0392b;
    color: #fff;
    border: none;
    padding: 0.6em 1.4em;
    border-radius: 4px;
    cursor: pointer;
    font-size: 1em;
    font-weight: bold;
  }
  button#refresh:hover { background: #a93226; }
  button#refresh:disabled { background: #aaa; cursor: not-allowed; }
  #status { color: #555; font-size: 0.9em; }
  .note {
    background: #fff8e1;
    border-left: 4px solid #f1c40f;
    padding: 1em;
    margin: 1.2em 0;
    border-radius: 4px;
    font-size: 0.9em;
  }
  .note strong { color: #b7790a; }
  .filter-input {
    padding: 0.4em 0.6em;
    border: 1px solid #ccc;
    border-radius: 4px;
    font-size: 0.95em;
    width: 240px;
  }
  .filter-controls {
    background: #fff;
    border: 1px solid #ddd;
    padding: 0.7em 0.9em;
    border-radius: 6px;
    margin: 0.6em 0;
    display: flex;
    flex-wrap: wrap;
    gap: 0.7em 1em;
    align-items: center;
    font-size: 0.9em;
  }
  .filter-controls label {
    display: inline-flex;
    align-items: center;
    gap: 0.3em;
    color: #444;
  }
  .filter-controls input[type=number] {
    padding: 0.25em 0.4em;
    border: 1px solid #bbb;
    border-radius: 3px;
    width: 60px;
    font-size: 0.95em;
  }
  .filter-controls select {
    padding: 0.25em 0.4em;
    border: 1px solid #bbb;
    border-radius: 3px;
    font-size: 0.95em;
  }
  .filter-controls button {
    background: #95a5a6;
    color: #fff;
    border: none;
    padding: 0.35em 0.9em;
    border-radius: 4px;
    cursor: pointer;
    font-size: 0.9em;
  }
  .filter-controls button:hover { background: #7f8c8d; }
  #filter-count { color: #555; margin-left: auto; font-weight: bold; }
  tbody tr.hidden { display: none; }
</style>
</head>
<body>

<h1>自社製品・自社事業の株主優待 一覧(プライム+スタンダード) <a href="/watchlist" style="font-size:0.6em;margin-left:1em;color:#2980b9;">→ 押し目買いリスト(★)</a></h1>

<div class="controls">
  <button id="refresh">価格を更新</button>
  <span id="status">起動時に自動取得します…</span>
  <input type="text" id="filter" class="filter-input" placeholder="キーワード絞り込み (スペース区切りAND)">
</div>

<div class="filter-controls">
  <label><input type="checkbox" id="f-starred-only"> ★のみ</label>
  <label><input type="checkbox" id="f-cheap-only"> 🟢 割安圏のみ</label>
  <label>評価
    <select id="f-rating">
      <option value="">全て</option>
      <option value="good">◎魅力的のみ</option>
      <option value="exclude-bad">✗除外</option>
      <option value="bad">✗微妙のみ</option>
      <option value="unrated">未評価のみ</option>
    </select>
  </label>
  <label>市場
    <select id="f-market">
      <option value="">全て</option>
      <option value="プライム">プライムのみ</option>
      <option value="スタンダード">スタンダードのみ</option>
    </select>
  </label>
  <label>PER ≤ <input type="number" id="f-per-max" value="25" step="1"> 倍</label>
  <label>ROE ≥ <input type="number" id="f-roe-min" value="5" step="1"> %</label>
  <label>ROE平均 ≥ <input type="number" id="f-roe-avg-min" placeholder="無" step="1"> %</label>
  <label>RSI ≤ <input type="number" id="f-rsi-max" placeholder="無" step="1"></label>
  <label>必要額 ≤ <input type="number" id="f-total-max" value="100" step="10"> 万円</label>
  <label>株価 ≤ <input type="number" id="f-price-max" placeholder="無" step="100"> 円</label>
  <button id="f-reset">条件リセット</button>
  <span id="filter-count"></span>
</div>

<div class="note">
  <strong>指標の見方:</strong>
  <strong>PER</strong>(株価収益率)= 株価 ÷ 1株あたり利益。10倍未満は<span style="color:#1e8449;font-weight:bold">割安</span>(緑)、30倍以上は<span style="color:#c0392b">割高</span>(赤)目安。
  <strong>ROE</strong>(自己資本利益率)= 純利益 ÷ 自己資本。15%以上は<span style="color:#1e8449;font-weight:bold">高収益</span>(緑)、5%未満は<span style="color:#999">低収益</span>(灰)。
  <strong>ROE平均</strong> = 直近4期(年次)の ROE の単純平均。現在ROEが平均の70%未満なら<span style="background:#fdebd0">下振れ</span>(オレンジ背景)、130%超なら<span style="background:#d4efdf">上振れ</span>(緑背景)で強調。
  <strong>RSI</strong>(週足14週)= 直近14週の週足から算出される買われ過ぎ・売られ過ぎ指標。70以上は<span style="color:#c0392b;font-weight:bold">買われ過ぎ</span>(赤)、30以下は<span style="color:#1e8449;font-weight:bold">売られ過ぎ</span>(緑)。<br>
  <strong>注意:</strong> 株価・指標は Yahoo Finance(yfinance 経由)。終値ベースのため取引時間中はズレあり。優待内容・最低株数は変更・廃止されることがあるため、必ず各社IRで最新情報をご確認ください。
</div>

<div id="tables"></div>

<!-- 分析編集モーダル -->
<div id="analysis-modal" class="modal-bg">
  <div class="modal">
    <h3 id="m-title">分析を編集</h3>
    <label>判定 <span id="m-judg-hint" style="font-weight:normal;color:#888;font-size:0.85em"></span></label>
    <select id="m-judgment">
      <option value="">— 自動計算を使用</option>
      <option value="buy">🟢 買い (手動)</option>
      <option value="hold">🟡 中立 (手動)</option>
      <option value="avoid">🔴 見送り (手動)</option>
    </select>
    <label>優待詳細(任意、Yahoo Finance等で確認した内容)</label>
    <div style="display:flex;gap:0.4em;align-items:center;flex-wrap:wrap;margin-bottom:0.3em">
      <input type="number" id="m-yutai-shares" placeholder="株数" style="width:80px" step="100">
      <span>株あたり</span>
      <input type="number" id="m-yutai-value" placeholder="円" style="width:90px" step="100">
      <span>円分の</span>
      <input type="text" id="m-yutai-item" placeholder="例: QUOカード" style="flex:1;min-width:140px">
    </div>
    <label>プロコメント</label>
    <textarea id="m-analysis" rows="4" placeholder="メモ・分析・気になる点を自由記述"></textarea>
    <div class="modal-actions">
      <button class="btn-cancel" id="m-cancel">キャンセル</button>
      <button class="btn-save" id="m-save">保存</button>
    </div>
  </div>
</div>

<script>
  const STOCKS = {{ stocks_json | safe }};

  function formatJPY(n) {
    if (n == null) return '—';
    return Math.round(n).toLocaleString('ja-JP') + ' 円';
  }

  function escapeHtml(s) {
    if (s == null) return '';
    return String(s).replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  }

  function judgmentBadge(j) {
    const labels = { buy: '🟢 買い', hold: '🟡 中立', avoid: '🔴 見送り' };
    if (!j) return '<span class="jbadge none">—</span>';
    return `<span class="jbadge ${j}">${labels[j] || j}</span>`;
  }

  // ===== ルールベース自動判定 (watchlist と同じ) =====
  const JR = {
    per(v) {
      if (v == null || v <= 0) return [-1, 'PER取得不能/赤字'];
      if (v < 12) return [+2, `PER ${v.toFixed(1)}<12 割安`];
      if (v < 18) return [+1, `PER ${v.toFixed(1)} 適正`];
      if (v < 25) return [ 0, `PER ${v.toFixed(1)} 普通`];
      if (v < 35) return [-1, `PER ${v.toFixed(1)} やや高`];
      if (v < 50) return [-2, `PER ${v.toFixed(1)} 高い`];
      return [-3, `PER ${v.toFixed(1)}>50 超割高`];
    },
    avgRoe(v) {
      if (v == null) return [0, 'ROE平均なし'];
      const p = v * 100;
      if (p >= 15) return [+2, `ROE平均 ${p.toFixed(1)}% 高収益`];
      if (p >= 8)  return [+1, `ROE平均 ${p.toFixed(1)}% 良好`];
      if (p >= 5)  return [ 0, `ROE平均 ${p.toFixed(1)}% 普通`];
      if (p >= 0)  return [-1, `ROE平均 ${p.toFixed(1)}% 低い`];
      return [-2, `ROE平均 ${p.toFixed(1)}% 構造赤字`];
    },
    roeChange(curr, avg) {
      if (curr == null || avg == null || avg <= 0) return [0, 'ROE比較不能'];
      if (curr < 0) return [-1, `ROE現在 ${(curr*100).toFixed(1)}% 赤字転落`];
      const ratio = curr / avg;
      if (ratio >= 1.0) return [+1, `ROE維持/改善 (${(ratio*100).toFixed(0)}%)`];
      if (ratio >= 0.5) return [ 0, `ROE低下 (平均の${(ratio*100).toFixed(0)}%)`];
      return [-1, `ROE大幅低下 (平均の${(ratio*100).toFixed(0)}%)`];
    },
    rsi(v) {
      if (v == null) return [0, 'RSIなし'];
      if (v <= 30) return [+2, `RSI ${v.toFixed(1)} 売られ過ぎ`];
      if (v <= 40) return [+1, `RSI ${v.toFixed(1)} 押し目`];
      if (v < 60)  return [ 0, `RSI ${v.toFixed(1)} 中性`];
      if (v < 70)  return [-1, `RSI ${v.toFixed(1)} やや過熱`];
      return [-2, `RSI ${v.toFixed(1)} 買われ過ぎ`];
    },
    confidence(c) {
      if (c === 'high') return [+1, '両指標トリガー(高確度)'];
      if (c === 'mid')  return [ 0, '片方トリガー(中確度)'];
      return [-1, '下値基準なし'];
    },
  };
  function computeJudgment(per, roe, avgRoe, rsi, conf) {
    const items = [
      JR.per(per), JR.avgRoe(avgRoe), JR.roeChange(roe, avgRoe),
      JR.rsi(rsi), JR.confidence(conf),
    ];
    const score = items.reduce((s, [p]) => s + p, 0);
    let label;
    if (score >= 2) label = 'buy';
    else if (score <= -2) label = 'avoid';
    else label = 'hold';
    return { label, score, items };
  }
  function judgmentBadgeAuto(s, per, roe, avgRoe, rsi, conf) {
    if (s.judgment) return judgmentBadge(s.judgment) + ' <small style="color:#888">(手動)</small>';
    const r = computeJudgment(per, roe, avgRoe, rsi, conf);
    const sign = r.score >= 0 ? '+' : '';
    const tip = r.items.map(([p, m]) => `${m} (${p >= 0 ? '+' : ''}${p})`).join(' / ') + ` = 合計${sign}${r.score}`;
    return `<span title="${escapeHtml(tip)}">${judgmentBadge(r.label)} <small>${sign}${r.score}</small></span>`;
  }

  // ===== 割安度判定 (11指標、減点法) - watchlist と共通ロジック =====
  function valuationLevel(price, high52, low52, sma200, rsi, per, pbr, divYield, peg, bbUpper, bbLower, avgRoe, roe, earningsGrowth, payoutRatio) {
    const items = [];
    const push = (score, label, value, full) => items.push({ score, label, value, full });
    if (high52 != null && low52 != null && high52 > low52 && price != null) {
      const pos = (price - low52) / (high52 - low52);
      const v = `${(pos*100).toFixed(0)}%`;
      if (pos >= 0.80) push(-2, '52週', v, `52週レンジ ${v} (高値圏 ≥80%)`);
      else             push( 0, '52週', v, `52週レンジ ${v} (ペナルティなし)`);
    } else push(0, '52週', '—', '52週レンジ計算不能');
    if (sma200 != null && price != null) {
      const dev = (price / sma200 - 1) * 100;
      const v = `${dev >= 0 ? '+' : ''}${dev.toFixed(1)}%`;
      if      (dev >= +5)   push(-2, 'SMA', v, `SMA200比 ${dev.toFixed(1)}% (大きく上 +5%以上)`);
      else if (dev >  +0.5) push(-1, 'SMA', v, `SMA200比 ${dev.toFixed(1)}% (上)`);
      else                  push( 0, 'SMA', v, `SMA200比 ${dev.toFixed(1)}% (下 or 接近、ペナルティなし)`);
    } else push(0, 'SMA', '—', 'SMA200なし');
    if (rsi == null) push(0, 'RSI週', '—', 'RSI週なし');
    else {
      const v = rsi.toFixed(1);
      if      (rsi >= 70) push(-2, 'RSI週', v, `RSI週 ${v} 買われ過ぎ`);
      else if (rsi >= 50) push(-1, 'RSI週', v, `RSI週 ${v} 強い`);
      else                push( 0, 'RSI週', v, `RSI週 ${v} (50未満、ペナルティなし)`);
    }
    if (bbUpper == null || bbLower == null || price == null) push(0, 'BB', '—', 'BBなし');
    else if (price >= bbUpper) push(-2, 'BB', '上限超', `BB上限超え ${Math.round(price)}≥${Math.round(bbUpper)}`);
    else if (price >= bbUpper * 0.98) push(-1, 'BB', '上限近', `BB上限近 ${Math.round(price)}`);
    else push(0, 'BB', '安全', 'BB上限から離れている');
    if (per == null || per <= 0) push(-2, 'PER', '—', 'PER赤字/取得不能(リスク)');
    else {
      const v = per.toFixed(1);
      if      (per >= 30) push(-2, 'PER', v, `PER ${v} 超割高(≥30)`);
      else if (per >= 20) push(-1, 'PER', v, `PER ${v} 高め(≥20)`);
      else                push( 0, 'PER', v, `PER ${v} (20未満、ペナルティなし)`);
    }
    if (pbr == null) push(-1, 'PBR', '—', 'PBR取得不能');
    else {
      const v = pbr.toFixed(2);
      if      (pbr >= 4) push(-2, 'PBR', v, `PBR ${v} 超高い(≥4)`);
      else if (pbr >= 2) push(-1, 'PBR', v, `PBR ${v} 高い(≥2)`);
      else                push( 0, 'PBR', v, `PBR ${v} (2未満、ペナルティなし)`);
    }
    if (divYield == null) push(-1, '配当', '—', '配当データなし');
    else {
      const v = `${divYield.toFixed(2)}%`;
      if      (divYield < 0.5) push(-2, '配当', v, `配当 ${v} 微配/無配`);
      else if (divYield < 1.5) push(-1, '配当', v, `配当 ${v} 低い`);
      else                      push( 0, '配当', v, `配当 ${v} (1.5%以上、ペナルティなし)`);
    }
    if (peg == null || peg <= 0) push(0, 'PEG', '—', 'PEGなし(ペナルティなし)');
    else {
      const v = peg.toFixed(2);
      if      (peg >= 3) push(-2, 'PEG', v, `PEG ${v} 超高い(≥3)`);
      else if (peg >= 2) push(-1, 'PEG', v, `PEG ${v} 高い(≥2)`);
      else                push( 0, 'PEG', v, `PEG ${v} (2未満、ペナルティなし)`);
    }
    if (earningsGrowth == null) push(0, '成長', '—', '業績成長データなし');
    else {
      const v = `${(earningsGrowth*100).toFixed(1)}%`;
      if      (earningsGrowth <= -0.10) push(-2, '成長', v, `業績成長 ${v} 大幅減益`);
      else if (earningsGrowth <  0)     push(-1, '成長', v, `業績成長 ${v} 減益`);
      else                              push( 0, '成長', v, `業績成長 ${v} (プラス)`);
    }
    if (payoutRatio == null) push(0, '配当性向', '—', '配当性向データなし');
    else {
      const v = `${(payoutRatio*100).toFixed(0)}%`;
      if      (payoutRatio > 1.0) push(-2, '配当性向', v, `配当性向 ${v} 赤字配当`);
      else if (payoutRatio > 0.8) push(-1, '配当性向', v, `配当性向 ${v} 高すぎ`);
      else                         push( 0, '配当性向', v, `配当性向 ${v} (持続性OK)`);
    }
    {
      let pen = 0; let detail = ''; let valDisp = '';
      if (avgRoe == null) { pen = -1; detail = 'ROE平均なし'; valDisp = '—'; }
      else {
        const apct = avgRoe * 100;
        valDisp = `${apct.toFixed(1)}%`;
        if (apct < 5) { pen -= 1; detail += `ROE平均${apct.toFixed(1)}%低い `; }
        if (roe != null && avgRoe > 0 && roe < avgRoe / 2) {
          pen -= 1; detail += `現在ROE${(roe*100).toFixed(1)}%大幅劣化`;
        }
        if (pen === 0) detail = `ROE平均${apct.toFixed(1)}% (質OK)`;
      }
      push(Math.max(-2, pen), 'ROE質', valDisp, detail);
    }
    const score = items.reduce((s, it) => s + it.score, 0);
    let label, cls;
    if (score >= -4)       { label = '🟢 割安圏'; cls = 'cheap'; }
    else if (score <= -13) { label = '🔴 割高圏'; cls = 'expensive'; }
    else                   { label = '🟡 適正圏'; cls = 'neutral'; }
    return { label, cls, score, items };
  }
  function premiumMark(score, avgRoe, earningsGrowth) {
    if (score === 0 && avgRoe != null && avgRoe >= 0.12) return '⭐';
    if (score >= -2 && avgRoe != null && avgRoe >= 0.12
        && earningsGrowth != null && earningsGrowth >= 0) return '☆';
    return '';
  }
  function valuationBadge(price, high52, low52, sma200, rsi, per, pbr, divYield, peg, bbUpper, bbLower, avgRoe, roe, earningsGrowth, payoutRatio) {
    const r = valuationLevel(price, high52, low52, sma200, rsi, per, pbr, divYield, peg, bbUpper, bbLower, avgRoe, roe, earningsGrowth, payoutRatio);
    const tip = r.items.map(it => `${it.full} (${it.score})`).join('\n') + `\n= 合計 ${r.score}(0が満点)`;
    const mark = premiumMark(r.score, avgRoe, earningsGrowth);
    const markHtml = mark ? ` <span class="premium" title="${mark === '⭐' ? '優良' : '準優良'}">${mark}</span>` : '';
    return `<span class="vbadge ${r.cls}" title="${escapeHtml(tip)}">${r.label} <small>${r.score}</small></span>${markHtml}`;
  }

  let _lastPrices = {}, _lastSma200 = {}, _lastRsi30 = {}, _lastRsis = {};
  let _lastHigh52 = {}, _lastLow52 = {};
  let _lastBbU = {}, _lastBbL = {}, _lastPers = {}, _lastPbrs = {}, _lastDivYields = {}, _lastPegs = {};
  let _lastAvgRoes = {}, _lastRoes = {}, _lastEarningsGrowths = {}, _lastPayoutRatios = {};

  function buildTables() {
    const byCat = {};
    for (const s of STOCKS) {
      (byCat[s.cat] ||= []).push(s);
    }
    const container = document.getElementById('tables');
    container.innerHTML = '';
    for (const [cat, list] of Object.entries(byCat)) {
      const h2 = document.createElement('h2');
      h2.textContent = cat;
      container.appendChild(h2);

      const table = document.createElement('table');
      table.innerHTML = `
        <thead><tr>
          <th data-key="sub">分野<span class="arrow"></span></th>
          <th data-key="market">市場<span class="arrow"></span></th>
          <th data-key="code">コード<span class="arrow"></span></th>
          <th data-key="name">企業名<span class="arrow"></span></th>
          <th data-key="yutai">主な優待内容<span class="arrow"></span></th>
          <th data-key="shares">最低株数<span class="arrow"></span></th>
          <th data-key="price">株価<span class="arrow"></span></th>
          <th data-key="high-down">高値比<span class="arrow"></span></th>
          <th data-key="total">必要額<span class="arrow"></span></th>
          <th data-key="per">PER<span class="arrow"></span></th>
          <th data-key="roe">ROE<span class="arrow"></span></th>
          <th data-key="roe-avg">ROE平均<span class="arrow"></span></th>
          <th data-key="rsi">RSI(週)<span class="arrow"></span></th>
          <th data-key="verified">最終確認<span class="arrow"></span></th>
          <th data-key="verify">確認</th>
          <th data-key="rating">評価</th>
          <th data-key="judgment">割安度<span class="arrow"></span></th>
          <th data-key="analysis">プロコメント</th>
        </tr></thead>
        <tbody></tbody>`;
      const tbody = table.querySelector('tbody');
      list.forEach((s, i) => {
        const tr = document.createElement('tr');
        tr.dataset.originalIndex = i;
        if (s.starred) tr.classList.add('starred');
        if (s.rating) tr.classList.add('rating-' + s.rating);
        tr.dataset.rating = s.rating || '';
        const badgeClass = s.market === 'プライム' ? 'badge-prime' : 'badge-standard';
        const starOn = s.starred ? 'on' : '';
        const starChar = s.starred ? '★' : '☆';
        const goodOn = s.rating === 'good' ? 'on' : '';
        const badOn  = s.rating === 'bad'  ? 'on' : '';
        tr.innerHTML = `
          <td>${s.sub}</td>
          <td><span class="badge ${badgeClass}">${s.market}</span></td>
          <td class="code">${s.code}</td>
          <td class="name"><button class="star-btn ${starOn}" data-code="${s.code}" title="注目銘柄に追加/解除">${starChar}</button> ${s.name}</td>
          <td>${s.yutai}</td>
          <td class="num">${s.min_shares}</td>
          <td class="num price loading" data-code="${s.code}">未取得</td>
          <td class="num high-down loading" data-code="${s.code}">—</td>
          <td class="num total loading" data-code="${s.code}" data-shares="${s.min_shares}">—</td>
          <td class="num per loading" data-code="${s.code}">—</td>
          <td class="num roe loading" data-code="${s.code}">—</td>
          <td class="num roe-avg loading" data-code="${s.code}">—</td>
          <td class="num rsi loading" data-code="${s.code}">—</td>
          <td class="verified loading" data-code="${s.code}">—</td>
          <td class="verify">
            <a href="https://finance.yahoo.co.jp/quote/${s.code}.T" target="_blank" rel="noopener">Y!</a>
            <a href="https://www.google.com/search?q=${encodeURIComponent(s.code + ' ' + s.name + ' 株主優待')}" target="_blank" rel="noopener">検索</a>
            <button class="verify-btn" data-code="${s.code}">確認済み</button>
          </td>
          <td class="rating-cell" data-code="${s.code}">
            <button class="rating-btn good ${goodOn}" data-rating="good" title="魅力的">◎</button>
            <button class="rating-btn bad ${badOn}" data-rating="bad" title="微妙">✗</button>
          </td>
          <td class="judgment" data-code="${s.code}">${judgmentBadge(s.judgment) || '<span class="jbadge none">計算中…</span>'}</td>
          <td class="analysis-cell" data-code="${s.code}">
            <span class="edit-link" data-code="${s.code}">編集</span>
            <span class="analysis-text">${s.analysis ? escapeHtml(s.analysis) : '<span style="color:#bbb">—</span>'}</span>
          </td>
        `;
        tr.dataset.search = tr.textContent.toLowerCase().replace(/\s+/g, ' ').trim();
        tbody.appendChild(tr);
      });
      attachSort(table);
      container.appendChild(table);
    }
  }

  function parseNum(s) {
    if (!s) return null;
    const m = s.replace(/[,\s円%]/g, '').match(/-?\d+(\.\d+)?/);
    return m ? parseFloat(m[0]) : null;
  }
  function isPlaceholder(s) {
    const t = (s || '').trim();
    return t === '' || t === '—' || t === '未取得' || t === '取得失敗';
  }

  function attachSort(table) {
    const ths = table.querySelectorAll('thead th');
    ths.forEach((th, idx) => {
      th.addEventListener('click', () => {
        const cur = th.dataset.dir || 'none';
        const next = cur === 'none' ? 'asc' : cur === 'asc' ? 'desc' : 'none';
        ths.forEach(t => {
          t.dataset.dir = '';
          t.querySelector('.arrow').textContent = '';
        });
        th.dataset.dir = next;
        th.querySelector('.arrow').textContent =
          next === 'asc' ? '▲' : next === 'desc' ? '▼' : '';

        const tbody = table.querySelector('tbody');
        const rows = Array.from(tbody.querySelectorAll('tr'));
        if (next === 'none') {
          rows.sort((a, b) =>
            parseInt(a.dataset.originalIndex) - parseInt(b.dataset.originalIndex)
          );
        } else {
          rows.sort((a, b) => {
            const aText = a.children[idx].textContent.trim();
            const bText = b.children[idx].textContent.trim();
            const aPh = isPlaceholder(aText);
            const bPh = isPlaceholder(bText);
            // プレースホルダは常に末尾
            if (aPh && bPh) return 0;
            if (aPh) return 1;
            if (bPh) return -1;
            const aN = parseNum(aText);
            const bN = parseNum(bText);
            let cmp;
            if (aN !== null && bN !== null) cmp = aN - bN;
            else cmp = aText.localeCompare(bText, 'ja');
            return next === 'asc' ? cmp : -cmp;
          });
        }
        rows.forEach(r => tbody.appendChild(r));
      });
    });
  }

  function refreshSearchIndex() {
    document.querySelectorAll('tbody tr').forEach(tr => {
      tr.dataset.search = tr.textContent.toLowerCase().replace(/\s+/g, ' ').trim();
    });
  }

  async function refreshPrices() {
    const btn = document.getElementById('refresh');
    const status = document.getElementById('status');
    btn.disabled = true;
    status.textContent = '取得中… (10〜30秒ほどかかります)';
    document.querySelectorAll('td.price, td.total, td.per, td.roe, td.roe-avg, td.rsi, td.verified, td.high-down').forEach(el => {
      el.classList.add('loading');
      el.classList.remove('error');
    });
    try {
      const res = await fetch('/api/prices');
      if (!res.ok) throw new Error('HTTP ' + res.status);
      const data = await res.json();
      const prices = data.prices || {};
      _lastPrices = prices;
      _lastSma200 = data.sma200s || {};
      _lastRsi30 = data.rsi30_prices || {};
      _lastRsis = data.rsis || {};
      _lastHigh52 = data.high52s || {};
      _lastLow52 = data.low52s || {};
      _lastBbU = data.bb_uppers || {};
      _lastBbL = data.bb_lowers || {};
      _lastPers = data.pers || {};
      _lastPbrs = data.pbrs || {};
      _lastDivYields = data.div_yields || {};
      _lastPegs = data.pegs || {};
      _lastAvgRoes = data.avg_roes || {};
      _lastRoes = data.roes || {};
      _lastEarningsGrowths = data.earnings_growths || {};
      _lastPayoutRatios = data.payout_ratios || {};
      const roes = data.roes || {};
      const pers = data.pers || {};
      const rsis = data.rsis || {};
      const avgRoes = data.avg_roes || {};
      const verified = data.verified || {};
      document.querySelectorAll('td.price').forEach(el => {
        const code = el.dataset.code;
        const p = prices[code];
        el.classList.remove('loading');
        if (p == null) {
          el.textContent = '取得失敗';
          el.classList.add('error');
        } else {
          el.textContent = p.toLocaleString('ja-JP', { maximumFractionDigits: 1 }) + ' 円';
        }
      });
      document.querySelectorAll('td.total').forEach(el => {
        const code = el.dataset.code;
        const shares = parseInt(el.dataset.shares, 10);
        const p = prices[code];
        el.classList.remove('loading');
        if (p == null) {
          el.textContent = '—';
          el.classList.add('error');
        } else {
          el.textContent = formatJPY(p * shares);
        }
      });
      document.querySelectorAll('td.per').forEach(el => {
        const code = el.dataset.code;
        const v = pers[code];
        el.classList.remove('loading', 'cheap', 'expensive', 'error');
        if (v == null || v <= 0) {
          el.textContent = '—';
        } else {
          el.textContent = v.toFixed(1) + ' 倍';
          if (v < 10) el.classList.add('cheap');
          else if (v >= 30) el.classList.add('expensive');
        }
      });
      document.querySelectorAll('td.roe').forEach(el => {
        const code = el.dataset.code;
        const r = roes[code];
        const ar = avgRoes[code];
        el.classList.remove('loading', 'high', 'low', 'error', 'below-avg', 'above-avg');
        if (r == null) {
          el.textContent = '—';
        } else {
          const pct = r * 100;
          el.textContent = pct.toFixed(1) + ' %';
          if (pct >= 15) el.classList.add('high');
          else if (pct < 5) el.classList.add('low');
          // 過去平均と比較: 70%未満なら下振れ強調、130%超なら上振れ強調
          if (ar != null && ar !== 0) {
            const ratio = r / ar;
            if (ratio < 0.7) el.classList.add('below-avg');
            else if (ratio > 1.3) el.classList.add('above-avg');
          }
        }
      });
      document.querySelectorAll('td.roe-avg').forEach(el => {
        const code = el.dataset.code;
        const r = avgRoes[code];
        el.classList.remove('loading', 'high', 'low', 'error');
        if (r == null) {
          el.textContent = '—';
        } else {
          const pct = r * 100;
          el.textContent = pct.toFixed(1) + ' %';
          if (pct >= 15) el.classList.add('high');
          else if (pct < 5) el.classList.add('low');
        }
      });
      document.querySelectorAll('td.rsi').forEach(el => {
        const code = el.dataset.code;
        const v = rsis[code];
        el.classList.remove('loading', 'overbought', 'oversold', 'error');
        if (v == null) {
          el.textContent = '—';
        } else {
          el.textContent = v.toFixed(1);
          if (v >= 70) el.classList.add('overbought');
          else if (v <= 30) el.classList.add('oversold');
        }
      });
      document.querySelectorAll('td.verified').forEach(el => {
        const code = el.dataset.code;
        renderVerified(el, verified[code]);
      });
      // 割安度セル更新
      document.querySelectorAll('td.judgment').forEach(el => {
        const code = el.dataset.code;
        el.innerHTML = valuationBadge(_lastPrices[code], _lastHigh52[code], _lastLow52[code], _lastSma200[code], _lastRsis[code], _lastPers[code], _lastPbrs[code], _lastDivYields[code], _lastPegs[code], _lastBbU[code], _lastBbL[code], _lastAvgRoes[code], _lastRoes[code], _lastEarningsGrowths[code], _lastPayoutRatios[code]);
      });
      // 高値比セル更新
      document.querySelectorAll('td.high-down').forEach(el => {
        const code = el.dataset.code;
        const p = _lastPrices[code], h = _lastHigh52[code];
        el.classList.remove('loading', 'high-down-med', 'high-down-large');
        if (p == null || h == null || h <= 0) {
          el.textContent = '—';
          return;
        }
        const down = (p - h) / h * 100;
        el.textContent = down.toFixed(1) + ' %';
        if (down <= -20) el.classList.add('high-down-large');
        else if (down <= -10) el.classList.add('high-down-med');
      });
      refreshSearchIndex();
      applyFilter();
      status.textContent = `最終更新: ${data.fetched_at}`;
    } catch (e) {
      status.textContent = '取得に失敗しました: ' + e.message;
    } finally {
      btn.disabled = false;
    }
  }

  function getNum(id) {
    const v = document.getElementById(id).value.trim();
    if (v === '') return NaN;
    return parseFloat(v);
  }

  const STALE_DAYS = 180;
  function renderVerified(el, dateStr) {
    el.classList.remove('loading', 'stale', 'fresh', 'error');
    if (!dateStr) {
      el.textContent = '未確認';
      el.classList.add('stale');
      return;
    }
    el.textContent = dateStr;
    const ageMs = Date.now() - new Date(dateStr).getTime();
    const ageDays = ageMs / (1000 * 60 * 60 * 24);
    if (ageDays > STALE_DAYS) el.classList.add('stale');
    else el.classList.add('fresh');
  }

  // ===== 分析編集モーダル =====
  let currentEditCode = null;
  function openModal(code) {
    const s = STOCKS.find(x => x.code === code);
    if (!s) return;
    currentEditCode = code;
    document.getElementById('m-title').textContent = `${s.code} ${s.name} の分析を編集`;
    document.getElementById('m-judgment').value = s.judgment || '';
    document.getElementById('m-analysis').value = s.analysis || '';
    document.getElementById('m-yutai-shares').value = s.yutai_shares != null ? s.yutai_shares : '';
    document.getElementById('m-yutai-value').value = s.yutai_value != null ? s.yutai_value : '';
    document.getElementById('m-yutai-item').value = s.yutai_item || '';
    document.getElementById('analysis-modal').classList.add('show');
  }
  function closeModal() {
    document.getElementById('analysis-modal').classList.remove('show');
    currentEditCode = null;
  }
  async function saveAnalysis() {
    if (!currentEditCode) return;
    const judgment = document.getElementById('m-judgment').value || null;
    const analysis = document.getElementById('m-analysis').value.trim() || null;
    const ys = document.getElementById('m-yutai-shares').value.trim();
    const yv = document.getElementById('m-yutai-value').value.trim();
    const yi = document.getElementById('m-yutai-item').value.trim() || null;
    try {
      const res = await fetch('/api/analysis/' + currentEditCode, {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({
          judgment, target_price: null, analysis,
          yutai_shares: ys === '' ? null : parseFloat(ys),
          yutai_value: yv === '' ? null : parseFloat(yv),
          yutai_item: yi,
        })
      });
      if (!res.ok) throw new Error('HTTP ' + res.status);
      const data = await res.json();
      // STOCKS のキャッシュも更新
      const s = STOCKS.find(x => x.code === currentEditCode);
      if (s) {
        s.judgment = data.judgment;
        s.analysis = data.analysis;
        s.yutai_shares = data.yutai_shares;
        s.yutai_value = data.yutai_value;
        s.yutai_item = data.yutai_item;
      }
      // セルを書き換え (判定列は割安度badge更新で上書きされるが、コメントは個別更新)
      document.querySelectorAll(`td.analysis-cell[data-code="${currentEditCode}"]`).forEach(el => {
        const text = data.analysis ? escapeHtml(data.analysis) : '<span style="color:#bbb">—</span>';
        el.innerHTML = `<span class="edit-link" data-code="${currentEditCode}">編集</span><span class="analysis-text">${text}</span>`;
      });
      const tr = document.querySelector(`td.judgment[data-code="${currentEditCode}"]`)?.closest('tr');
      if (tr) tr.dataset.search = tr.textContent.toLowerCase().replace(/\s+/g, ' ').trim();
      closeModal();
    } catch (e) {
      alert('保存失敗: ' + e.message);
    }
  }
  document.getElementById('m-cancel').addEventListener('click', closeModal);
  document.getElementById('m-save').addEventListener('click', saveAnalysis);
  document.getElementById('analysis-modal').addEventListener('click', (e) => {
    if (e.target.id === 'analysis-modal') closeModal();
  });

  async function setRating(code, value, btn) {
    btn.disabled = true;
    try {
      const res = await fetch('/api/rating/' + code, {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({rating: value})
      });
      if (!res.ok) throw new Error('HTTP ' + res.status);
      const data = await res.json();
      const cell = document.querySelector(`td.rating-cell[data-code="${code}"]`);
      if (cell) {
        cell.querySelectorAll('.rating-btn').forEach(b => {
          b.classList.toggle('on', b.dataset.rating === data.rating);
        });
      }
      const tr = btn.closest('tr');
      if (tr) {
        tr.classList.remove('rating-good', 'rating-bad');
        if (data.rating) tr.classList.add('rating-' + data.rating);
        tr.dataset.rating = data.rating || '';
      }
      applyFilter();
    } catch (e) {
      alert('評価更新失敗: ' + e.message);
    } finally {
      btn.disabled = false;
    }
  }

  async function toggleStar(code, btn) {
    btn.disabled = true;
    try {
      const res = await fetch('/api/star/' + code, { method: 'POST' });
      if (!res.ok) throw new Error('HTTP ' + res.status);
      const data = await res.json();
      btn.textContent = data.starred ? '★' : '☆';
      btn.classList.toggle('on', data.starred);
      const tr = btn.closest('tr');
      if (tr) tr.classList.toggle('starred', data.starred);
      applyFilter();
    } catch (e) {
      alert('★更新失敗: ' + e.message);
    } finally {
      btn.disabled = false;
    }
  }

  async function markVerified(code, btn) {
    btn.disabled = true;
    try {
      const res = await fetch('/api/verify/' + code, { method: 'POST' });
      if (!res.ok) throw new Error('HTTP ' + res.status);
      const data = await res.json();
      const cell = document.querySelector(`td.verified[data-code="${code}"]`);
      if (cell) renderVerified(cell, data.last_verified);
      // 検索インデックスとフィルタを更新
      const tr = btn.closest('tr');
      if (tr) tr.dataset.search = tr.textContent.toLowerCase().replace(/\s+/g, ' ').trim();
      applyFilter();
    } catch (e) {
      alert('確認済み更新に失敗: ' + e.message);
    } finally {
      btn.disabled = false;
    }
  }

  function applyFilter() {
    const q = document.getElementById('filter').value.trim().toLowerCase();
    const tokens = q ? q.split(/\s+/).filter(Boolean) : [];
    const fStarOnly = document.getElementById('f-starred-only').checked;
    const fCheap    = document.getElementById('f-cheap-only').checked;
    const fRating   = document.getElementById('f-rating').value;
    const fMarket  = document.getElementById('f-market').value;
    const fPerMax  = getNum('f-per-max');
    const fRoeMin  = getNum('f-roe-min');
    const fRoeAvgMin = getNum('f-roe-avg-min');
    const fRsiMax  = getNum('f-rsi-max');
    const fTotMax  = getNum('f-total-max');   // 単位: 万円
    const fPriMax  = getNum('f-price-max');   // 単位: 円

    let total = 0, shown = 0;
    document.querySelectorAll('tbody tr').forEach(tr => {
      total++;
      let hide = false;

      // 0a. ★のみ
      if (fStarOnly && !tr.classList.contains('starred')) hide = true;

      // 0a-2. 🟢 割安圏のみ (割安度バッジが cheap のもの)
      if (!hide && fCheap) {
        const vbadge = tr.querySelector('td.judgment .vbadge');
        if (!vbadge || !vbadge.classList.contains('cheap')) hide = true;
      }

      // 0b. 評価フィルタ
      if (!hide && fRating) {
        const r = tr.dataset.rating || '';
        if      (fRating === 'good'        && r !== 'good') hide = true;
        else if (fRating === 'bad'         && r !== 'bad')  hide = true;
        else if (fRating === 'exclude-bad' && r === 'bad')  hide = true;
        else if (fRating === 'unrated'     && r !== '')     hide = true;
      }

      // 1. キーワード(全列AND)
      if (!hide) {
        const hay = tr.dataset.search || '';
        if (tokens.length && !tokens.every(t => hay.includes(t))) hide = true;
      }

      if (!hide) {
        const c = tr.children;
        // 列順: 0:分野 1:市場 2:コード 3:企業名 4:優待 5:最低株数 6:株価 7:高値比 8:必要額 9:PER 10:ROE 11:ROE平均 12:RSI(週) 13:最終確認 14:確認 15:評価 16:割安度 17:プロコメント
        const market = c[1].textContent.trim();
        const price  = parseNum(c[6].textContent);
        const total_ = parseNum(c[8].textContent);   // 円(高値比挿入で1つ右シフト)
        const per    = parseNum(c[9].textContent);
        const roe    = parseNum(c[10].textContent);  // %
        const roeAvg = parseNum(c[11].textContent);  // %
        const rsi    = parseNum(c[12].textContent);

        if (fMarket && market !== fMarket) hide = true;
        // 数値フィルタ: データがない(null)場合は条件不一致として除外
        if (!hide && !isNaN(fPerMax) && (per === null || per > fPerMax)) hide = true;
        if (!hide && !isNaN(fRoeMin) && (roe === null || roe < fRoeMin)) hide = true;
        if (!hide && !isNaN(fRoeAvgMin) && (roeAvg === null || roeAvg < fRoeAvgMin)) hide = true;
        if (!hide && !isNaN(fRsiMax) && (rsi === null || rsi > fRsiMax)) hide = true;
        if (!hide && !isNaN(fTotMax) && (total_ === null || total_ > fTotMax * 10000)) hide = true;
        if (!hide && !isNaN(fPriMax) && (price === null || price > fPriMax)) hide = true;
      }

      tr.classList.toggle('hidden', hide);
      if (!hide) shown++;
    });

    // 行が全部消えたカテゴリ見出し(h2)も非表示にする
    document.querySelectorAll('#tables > h2').forEach(h2 => {
      const table = h2.nextElementSibling;
      if (!table) return;
      const visible = table.querySelectorAll('tbody tr:not(.hidden)').length;
      h2.style.display = visible === 0 ? 'none' : '';
      table.style.display = visible === 0 ? 'none' : '';
    });

    document.getElementById('filter-count').textContent = `${shown} / ${total} 件表示`;
  }

  function resetFilter() {
    document.getElementById('filter').value = '';
    document.getElementById('f-starred-only').checked = false;
    document.getElementById('f-cheap-only').checked = false;
    document.getElementById('f-rating').value = '';
    document.getElementById('f-market').value = '';
    document.getElementById('f-per-max').value = '';
    document.getElementById('f-roe-min').value = '';
    document.getElementById('f-roe-avg-min').value = '';
    document.getElementById('f-rsi-max').value = '';
    document.getElementById('f-total-max').value = '';
    document.getElementById('f-price-max').value = '';
    applyFilter();
  }

  document.getElementById('refresh').addEventListener('click', refreshPrices);
  document.getElementById('filter').addEventListener('input', applyFilter);
  document.getElementById('f-starred-only').addEventListener('change', applyFilter);
  document.getElementById('f-cheap-only').addEventListener('change', applyFilter);
  document.getElementById('f-rating').addEventListener('change', applyFilter);
  ['f-market','f-per-max','f-roe-min','f-roe-avg-min','f-rsi-max','f-total-max','f-price-max'].forEach(id => {
    document.getElementById(id).addEventListener('input', applyFilter);
    document.getElementById(id).addEventListener('change', applyFilter);
  });
  document.getElementById('f-reset').addEventListener('click', resetFilter);

  // ★/評価/確認済み/編集 ボタン: イベント委譲
  document.getElementById('tables').addEventListener('click', (e) => {
    const star = e.target.closest('button.star-btn');
    if (star) { toggleStar(star.dataset.code, star); return; }
    const rating = e.target.closest('button.rating-btn');
    if (rating) {
      const code = rating.closest('td.rating-cell').dataset.code;
      const isOn = rating.classList.contains('on');
      const newVal = isOn ? null : rating.dataset.rating;
      setRating(code, newVal, rating);
      return;
    }
    const editLink = e.target.closest('.edit-link');
    if (editLink) { openModal(editLink.dataset.code); return; }
    const btn = e.target.closest('button.verify-btn');
    if (btn) markVerified(btn.dataset.code, btn);
  });

  buildTables();
  refreshPrices();
</script>

</body>
</html>
"""


WATCHLIST_HTML = r"""<!DOCTYPE html>
<html lang="ja">
<head>
<meta charset="UTF-8">
<title>押し目買いウォッチリスト ★</title>
<style>
  body {
    font-family: -apple-system, "Hiragino Kaku Gothic ProN", "Yu Gothic", sans-serif;
    max-width: 1400px;
    margin: 1.5em auto;
    padding: 0 1em;
    line-height: 1.55;
    color: #222;
    background: #fafafa;
  }
  h1 {
    border-bottom: 3px solid #c0392b;
    padding-bottom: 0.3em;
    font-size: 1.4em;
  }
  h1 a { font-size: 0.6em; margin-left: 1em; color: #2980b9; text-decoration: none; }
  .view-tabs {
    display: flex; gap: 0.4em; margin: 0 0 1em;
    border-bottom: 2px solid #ddd;
  }
  .view-tab {
    appearance: none; background: #ecf0f1; color: #555;
    border: 1px solid #ddd; border-bottom: none;
    border-radius: 6px 6px 0 0; padding: 0.55em 1.2em;
    font: inherit; font-weight: bold; cursor: pointer;
  }
  .view-tab:hover { background: #f8e9e7; color: #922b21; }
  .view-tab.active { background: #c0392b; color: #fff; border-color: #c0392b; }
  .view-panel[hidden] { display: none; }
  .stock-list-summary {
    margin: 0 0 0.8em; color: #555; font-size: 0.92em;
  }
  .stock-table-wrap {
    overflow-x: auto; background: #fff; border: 1px solid #ddd;
    border-radius: 6px; box-shadow: 0 1px 3px rgba(0,0,0,0.06);
  }
  .stock-table { width: 100%; border-collapse: collapse; font-size: 0.9em; }
  .stock-table th, .stock-table td {
    padding: 0.65em 0.8em; border-bottom: 1px solid #eee; text-align: left;
  }
  .stock-table th { background: #f4ebe9; white-space: nowrap; color: #5d2b26; }
  .stock-table tbody tr:last-child td { border-bottom: none; }
  .stock-table tbody tr:hover { background: #fff8f7; }
  .stock-table .num { width: 3em; text-align: right; color: #888; }
  .stock-table .code { font-family: "SF Mono", Menlo, monospace; white-space: nowrap; }
  .stock-table .name { font-weight: bold; min-width: 12em; }
  .topbar {
    position: sticky; top: 0; background: #fafafa; z-index: 10;
    padding: 0.6em 0; border-bottom: 1px solid #eee;
    display: flex; gap: 1em; align-items: center; flex-wrap: wrap;
  }
  button#refresh {
    background: #c0392b; color: #fff; border: none;
    padding: 0.55em 1.3em; border-radius: 4px; cursor: pointer;
    font-size: 0.95em; font-weight: bold;
  }
  button#refresh:hover { background: #a93226; }
  button#refresh:disabled { background: #aaa; cursor: not-allowed; }
  #status { color: #555; font-size: 0.9em; }
  .summary {
    background: #fff8e1; border-left: 4px solid #f1c40f;
    padding: 0.7em 1em; margin: 1em 0; border-radius: 4px; font-size: 0.92em;
  }
  .grid { display: flex; flex-direction: column; gap: 0.5em; }
  .subgrid {
    display: grid;
    grid-template-columns: repeat(auto-fill, minmax(360px, 1fr));
    gap: 0.9em;
    margin-bottom: 1em;
  }
  .section-header h2 {
    margin: 1em 0 0.2em; padding: 0.3em 0.5em;
    font-size: 1.05em; border-bottom: 2px solid #c0392b; color: #c0392b;
  }
  .section-header h2 .count { color: #888; font-size: 0.85em; font-weight: normal; }
  .section-header .section-desc { margin: 0 0 0.3em; font-size: 0.85em; color: #666; }
  .card {
    border: 2px solid #ddd; border-radius: 8px;
    padding: 0.9em 1em; background: #fff;
    box-shadow: 0 1px 3px rgba(0,0,0,0.06);
    display: flex; flex-direction: column; gap: 0.5em;
  }
  .card-head {
    display: flex; align-items: center; gap: 0.5em; flex-wrap: wrap;
  }
  .card-head .code { font-family: "SF Mono", Menlo, monospace; color: #555; font-size: 0.9em; }
  .card-head .name { font-weight: bold; font-size: 1.05em; }
  .card-head .badge {
    display: inline-block; padding: 1px 7px; border-radius: 3px;
    font-size: 0.75em; font-weight: bold; color: #fff;
  }
  .badge-prime { background: #2e86c1; }
  .badge-standard { background: #95a5a6; }
  .vbadge {
    display: inline-block; padding: 2px 9px; border-radius: 3px;
    font-size: 0.92em; font-weight: bold; color: #fff;
  }
  .vbadge.cheap { background: #27ae60; }
  .vbadge.neutral { background: #95a5a6; }
  .vbadge.expensive { background: #c0392b; }
  .premium { font-size: 1.15em; vertical-align: middle; cursor: help; }
  .yield-block {
    background: #f5f7fa; padding: 0.5em 0.7em; border-radius: 4px;
    border-left: 3px solid #95a5a6; line-height: 1.45;
  }
  .yield-block.high { background: #d5f5e3; border-left-color: #27ae60; }
  .yield-block.mid { background: #fcf3cf; border-left-color: #f1c40f; }
  .yield-block.placeholder { font-size: 0.83em; color: #999; font-style: italic; border-left-color: #ddd; }
  .yield-block .ty-num { font-size: 1.15em; }
  .yield-block.high .ty-num { color: #196f3d; }
  .yield-block.mid .ty-num { color: #b7790a; }
  .high-down-med { color: #d35400; font-weight: bold; }
  .high-down-large { color: #c0392b; font-weight: bold; background: #fdebd0; padding: 1px 6px; border-radius: 3px; }
  .ic-grid {
    display: grid;
    grid-template-columns: repeat(4, 1fr);
    gap: 4px;
    margin: 0.4em 0;
  }
  .ic {
    padding: 5px 4px; border-radius: 4px; text-align: center;
    line-height: 1.15; cursor: help;
  }
  .ic-name { font-size: 0.72em; font-weight: bold; opacity: 0.85; }
  .ic-val { font-family: "SF Mono", Menlo, monospace; font-size: 0.85em; margin-top: 1px; }
  .ic-pts { font-size: 0.7em; opacity: 0.7; margin-top: 1px; }
  .ic-good-strong { background: #27ae60; color: #fff; }
  .ic-good        { background: #abebc6; color: #196f3d; }
  .ic-neutral     { background: #ecf0f1; color: #555; }
  .ic-bad         { background: #fadbd8; color: #922b21; }
  .ic-bad-strong  { background: #c0392b; color: #fff; }
  .jbadge {
    display: inline-block; padding: 1px 8px; border-radius: 3px;
    font-size: 0.85em; font-weight: bold; color: #fff;
  }
  .jbadge.buy { background: #27ae60; }
  .jbadge.hold { background: #f39c12; }
  .jbadge.avoid { background: #c0392b; }
  .jbadge.none { background: #ecf0f1; color: #888; font-weight: normal; }
  .star-btn {
    margin-left: auto; background: none; border: none; cursor: pointer;
    font-size: 1.3em; color: #f1c40f; padding: 0;
  }
  .star-btn:hover { transform: scale(1.15); }
  .price-row {
    display: flex; gap: 1em; align-items: baseline; font-size: 1.05em;
  }
  .price-row .label { color: #666; font-size: 0.85em; }
  .price-row strong { font-size: 1.15em; }
  .metrics {
    font-size: 0.83em; color: #555;
    display: flex; gap: 0.8em; flex-wrap: wrap;
  }
  .metrics span { white-space: nowrap; }
  .comment {
    font-size: 0.85em; color: #444; line-height: 1.4;
    background: #fafafa; padding: 0.5em 0.7em; border-radius: 4px;
    border-left: 3px solid #ccc;
  }
  .actions {
    display: flex; gap: 0.4em; flex-wrap: wrap; margin-top: auto;
  }
  .actions a, .actions button {
    background: #ecf0f1; color: #2c3e50; text-decoration: none;
    padding: 3px 9px; border-radius: 3px; font-size: 0.85em;
    border: none; cursor: pointer;
  }
  .actions a:hover, .actions button:hover { background: #d5dbdb; }
  .empty {
    text-align: center; padding: 3em; color: #888;
    background: #fff; border-radius: 6px; border: 1px dashed #ccc;
  }
  /* モーダル */
  .modal-bg {
    display: none; position: fixed; inset: 0;
    background: rgba(0,0,0,0.5); z-index: 1000;
    align-items: center; justify-content: center;
  }
  .modal-bg.show { display: flex; }
  .modal {
    background: #fff; padding: 1.5em; border-radius: 8px;
    width: 480px; max-width: 90%;
    box-shadow: 0 10px 30px rgba(0,0,0,0.3);
  }
  .modal h3 { margin: 0 0 0.8em; color: #c0392b; }
  .modal label { display: block; margin: 0.6em 0 0.2em; font-weight: bold; font-size: 0.9em; }
  .modal input, .modal select, .modal textarea {
    width: 100%; padding: 0.4em; border: 1px solid #bbb; border-radius: 3px;
    font-size: 0.95em; box-sizing: border-box;
  }
  .modal textarea { resize: vertical; font-family: inherit; min-height: 80px; }
  .modal-actions { margin-top: 1em; text-align: right; }
  .modal-actions button {
    padding: 0.5em 1em; margin-left: 0.5em; border: none;
    border-radius: 4px; cursor: pointer;
  }
  .modal-actions .btn-save { background: #27ae60; color: #fff; }
  .modal-actions .btn-cancel { background: #95a5a6; color: #fff; }
  #watchlist-view .ic-grid { grid-template-columns: repeat(4, 1fr); }
  .ic-rsi-sma.above { background: #e67e22; color: #fff; }
  .ic-rsi-sma.below { background: #2980b9; color: #fff; }
  .ic-rsi-sma.equal { background: #7f8c8d; color: #fff; }
  .bb-z { font-size: 0.68em; margin-top: 3px; opacity: 0.85; }
  .comparison-block {
    display: flex; align-items: center; gap: 0.55em; flex-wrap: wrap;
    padding: 0.45em 0.65em; border-radius: 4px;
    border-left: 3px solid #7f8c8d; background: #f4f6f7;
    font-size: 0.84em; line-height: 1.35;
  }
  .comparison-block .comparison-metric { white-space: nowrap; }
  .comparison-block .comparison-name { color: #666; font-weight: bold; }
  .comparison-block .comparison-change { font-family: "SF Mono", Menlo, monospace; font-weight: bold; }
  .comparison-block .comparison-change.below { color: #2980b9; }
  .comparison-block .comparison-change.above { color: #d35400; }
  .comparison-block .comparison-change.flat { color: #7f8c8d; }
  .comparison-block .comparison-sep { color: #aaa; }
  .comparison-block .trend-tag { margin-left: auto; font-weight: bold; white-space: nowrap; }
  .comparison-block.long-slump { background: #eaecee; border-left-color: #34495e; color: #2c3e50; }
  .comparison-block.recovery-watch { background: #f4ecf7; border-left-color: #8e44ad; color: #6c3483; }
</style>
</head>
<body>

<h1>★ 押し目買いウォッチリスト</h1>

<div class="view-tabs" role="tablist" aria-label="表示切り替え">
  <button type="button" class="view-tab active" id="tab-watchlist" role="tab" aria-selected="true" aria-controls="watchlist-view" data-view="watchlist-view">押し目買いリスト</button>
  <button type="button" class="view-tab" id="tab-stock-list" role="tab" aria-selected="false" aria-controls="stock-list-view" data-view="stock-list-view">銘柄一覧 <span id="stock-tab-count"></span></button>
</div>

<section id="watchlist-view" class="view-panel" role="tabpanel" aria-labelledby="tab-watchlist">
<div class="topbar">
  <button id="refresh">価格を更新</button>
  <span id="status">起動時に自動取得します…</span>
  <span style="margin-left:auto"></span>
  <label style="font-size:0.9em">
    52週レンジ位置 ≤ <input type="number" id="f-rangepos-max" value="30" step="5" min="0" max="100" style="width:60px;padding:0.25em 0.4em;border:1px solid #bbb;border-radius:3px"> %
  </label>
  <button id="f-clear" style="padding:0.35em 0.8em;background:#95a5a6;color:#fff;border:none;border-radius:3px;cursor:pointer;font-size:0.85em">条件クリア</button>
</div>

<div class="summary" id="summary">
  <strong>判定方式: 全<span id="watchlist-total"></span>銘柄を同じ株価履歴から計算できる3指標</strong><br>
  <span style="font-size:0.85em">
  52週レンジ・週足RSI・ボリンジャーバンドのみ。3年前比・長期比・利回りは参考表示。ただし3年比と長期比が両方−3%未満の銘柄は最下部の長期低迷枠へ分離。<br>
  RSIは現在値と14SMAを別ブロック表示。オレンジ = RSIが14SMAより上 / 青 = 下（比較自体は判定外）。<br>
  BBは現在値が「-1σ〜0σ」「0σ〜+1σ」「+1σ〜+2σ」など、どの区間にいるかを表示。<br>
  チップ色: <span style="background:#27ae60;color:#fff;padding:1px 6px;border-radius:3px">✓ 0</span> = ペナルティなし /
  <span style="background:#fadbd8;color:#922b21;padding:1px 6px;border-radius:3px">-1</span> = 軽い減点 /
  <span style="background:#c0392b;color:#fff;padding:1px 6px;border-radius:3px">-2</span> = 重い減点
  </span>
</div>

<div id="grid" class="grid"></div>
</section>

<section id="stock-list-view" class="view-panel" role="tabpanel" aria-labelledby="tab-stock-list" hidden>
  <p class="stock-list-summary" id="stock-list-summary"></p>
  <div class="stock-table-wrap">
    <table class="stock-table">
      <thead>
        <tr>
          <th class="num">#</th>
          <th>コード</th>
          <th>銘柄名</th>
          <th>市場</th>
          <th>カテゴリ</th>
          <th>分類</th>
        </tr>
      </thead>
      <tbody id="stock-list-body"></tbody>
    </table>
  </div>
</section>

<!-- 編集モーダル -->
<div id="analysis-modal" class="modal-bg">
  <div class="modal">
    <h3 id="m-title">分析を編集</h3>
    <label>判定 <span id="m-judg-hint" style="font-weight:normal;color:#888;font-size:0.85em"></span></label>
    <select id="m-judgment">
      <option value="">— 自動計算を使用</option>
      <option value="buy">🟢 買い (手動)</option>
      <option value="hold">🟡 中立 (手動)</option>
      <option value="avoid">🔴 見送り (手動)</option>
    </select>
    <label>優待詳細(任意、Yahoo Finance等で確認した内容)</label>
    <div style="display:flex;gap:0.4em;align-items:center;flex-wrap:wrap;margin-bottom:0.3em">
      <input type="number" id="m-yutai-shares" placeholder="株数" style="width:80px" step="100">
      <span>株あたり</span>
      <input type="number" id="m-yutai-value" placeholder="円" style="width:90px" step="100">
      <span>円分の</span>
      <input type="text" id="m-yutai-item" placeholder="例: QUOカード" style="flex:1;min-width:140px">
    </div>
    <label>プロコメント</label>
    <textarea id="m-analysis" rows="4" placeholder="メモ・分析・気になる点を自由記述"></textarea>
    <div class="modal-actions">
      <button class="btn-cancel" id="m-cancel">キャンセル</button>
      <button class="btn-save" id="m-save">保存</button>
    </div>
  </div>
</div>

<script>
  const STOCKS = {{ stocks_json | safe }};
  let _prices = {}, _rsis = {}, _rsiSma14s = {};
  let _high52s = {}, _low52s = {};
  let _bbUppers = {}, _bbLowers = {};
  let _price3yRefs = {}, _price3yChanges = {};
  let _longRefs = {}, _longChanges = {}, _longLabels = {}, _longYears = {};
  let _divYields = {};

  function escapeHtml(s) {
    if (s == null) return '';
    return String(s).replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  }
  function fmt(n, suf='') { return n == null ? '—' : Math.round(n).toLocaleString('ja-JP') + suf; }

  function renderStockList() {
    const sorted = [...STOCKS].sort((a, b) =>
      String(a.code).localeCompare(String(b.code), 'ja', { numeric: true })
    );
    document.getElementById('stock-tab-count').textContent = `(${STOCKS.length})`;
    document.getElementById('watchlist-total').textContent = STOCKS.length;
    document.getElementById('stock-list-summary').innerHTML =
      `<strong>${STOCKS.length}銘柄</strong>が押し目買いリストに反映されています。`;
    document.getElementById('stock-list-body').innerHTML = sorted.map((s, index) => `
      <tr>
        <td class="num">${index + 1}</td>
        <td class="code">${escapeHtml(s.code)}</td>
        <td class="name">${escapeHtml(s.name)}</td>
        <td>${escapeHtml(s.market || '—')}</td>
        <td>${escapeHtml(s.cat || '—')}</td>
        <td>${escapeHtml(s.sub || '—')}</td>
      </tr>
    `).join('');
  }

  function switchView(viewId) {
    document.querySelectorAll('.view-tab').forEach(tab => {
      const active = tab.dataset.view === viewId;
      tab.classList.toggle('active', active);
      tab.setAttribute('aria-selected', active ? 'true' : 'false');
    });
    document.querySelectorAll('.view-panel').forEach(panel => {
      panel.hidden = panel.id !== viewId;
    });
  }

  // ===== 全銘柄で同じ条件になる株価指標 (3指標、減点法) =====
  // 同じ株価履歴から計算した「52週レンジ・週足RSI・BB」の過熱サインだけを減点する。
  function valuationLevel(price, high52, low52, rsi, rsiSma14, bbUpper, bbLower) {
    const items = [];
    const push = (score, label, value, full, extra = {}) =>
      items.push({ score, label, value, full, ...extra });
    // 1. 52週レンジ位置
    if (high52 != null && low52 != null && high52 > low52 && price != null) {
      const pos = (price - low52) / (high52 - low52);
      const pct = (pos * 100).toFixed(0);
      const v = `${pct}%`;
      if      (pos >= 0.80) push(-2, '52週', v, `52週レンジ ${pct}% (高値圏 ≥80%)`);
      else if (pos >  0.30) push(-1, '52週', v, `52週レンジ ${pct}% (押し目基準外 >30%)`);
      else if (pos <= 0.10) push( 0, '52週', v, `52週レンジ ${pct}% (強い押し目 ≤10%)`);
      else if (pos <= 0.20) push( 0, '52週', v, `52週レンジ ${pct}% (深い押し目 ≤20%)`);
      else if (pos <= 0.30) push( 0, '52週', v, `52週レンジ ${pct}% (押し目 ≤30%)`);
      else                  push( 0, '52週', v, `52週レンジ ${pct}% (ペナルティなし)`);
    } else push(0, '52週', '—', '52週レンジ計算不能');
    // 2. 週足RSI: 50以上で減点
    if (rsi == null) push(0, 'RSI週', '—', 'RSI週計算不能');
    else {
      const v = rsi.toFixed(1);
      let relation = 'unknown', relationLabel = '比較不能', diff = null;
      if (rsiSma14 != null) {
        diff = rsi - rsiSma14;
        if (diff > 0.05) { relation = 'above'; relationLabel = '▲ 上'; }
        else if (diff < -0.05) { relation = 'below'; relationLabel = '▼ 下'; }
        else { relation = 'equal'; relationLabel = '＝ 同水準'; }
      }
      const smaDetail = rsiSma14 != null
        ? ` / RSI 14SMA ${rsiSma14.toFixed(1)}より${relationLabel}` : '';
      const extra = { rsiSma14, relation, relationLabel, diff };
      if      (rsi >= 70) push(-2, 'RSI週', v, `RSI週 ${v} 買われ過ぎ${smaDetail}`, extra);
      else if (rsi >= 50) push(-1, 'RSI週', v, `RSI週 ${v} 強い${smaDetail}`, extra);
      else                push( 0, 'RSI週', v, `RSI週 ${v} (50未満、ペナルティなし)${smaDetail}`, extra);
    }
    // 3. ボリンジャーバンド位置: 現在値がどのσ区間にいるかを表示
    if (bbUpper == null || bbLower == null || price == null) {
      push(0, 'BB', '—', 'BB計算不能');
    } else {
      const middle = (bbUpper + bbLower) / 2;
      const sigma = (bbUpper - bbLower) / 4;
      if (sigma <= 0) {
        push(0, 'BB', '—', 'BB標準偏差を計算不能');
      } else {
        const z = (price - middle) / sigma;
        let band;
        if      (z >=  2) band = '+2σ以上';
        else if (z >=  1) band = '+1σ〜+2σ';
        else if (z >=  0) band = '0σ〜+1σ';
        else if (z >= -1) band = '-1σ〜0σ';
        else if (z >= -2) band = '-2σ〜-1σ';
        else               band = '-2σ以下';
        const score = z >= 2 ? -2 : z >= 1 ? -1 : 0;
        const signedZ = `${z >= 0 ? '+' : ''}${z.toFixed(2)}σ`;
        push(score, 'BB', band, `BB位置 ${band} (現在 ${signedZ})`, { bbZ: z, signedZ });
      }
    }
    const score = items.reduce((s, it) => s + it.score, 0);
    let label, cls;
    if (score === 0)       { label = '🟢 押し目候補'; cls = 'cheap'; }
    else if (score >= -2)  { label = '🟡 反発・注意'; cls = 'neutral'; }
    else                   { label = '🔴 過熱気味'; cls = 'expensive'; }
    return { label, cls, score, items };
  }
  function valuationBadge(price, high52, low52, rsi, rsiSma14, bbUpper, bbLower) {
    const r = valuationLevel(price, high52, low52, rsi, rsiSma14, bbUpper, bbLower);
    const tip = r.items.map(it => `${it.full} (${it.score})`).join('\n') + `\n= 合計 ${r.score}`;
    return `<span class="vbadge ${r.cls}" title="${escapeHtml(tip)}">${r.label} <small>${r.score}</small></span>`;
  }
  function isLongSlump(code) {
    const change3y = _price3yChanges[code];
    const longChange = _longChanges[code];
    return change3y != null && longChange != null && change3y < -3 && longChange < -3;
  }
  function comparisonChangeHtml(changePct) {
    if (changePct == null) return '<span class="comparison-change flat">—</span>';
    let cls, arrow;
    if (changePct < -3) { cls = 'below'; arrow = '▼'; }
    else if (changePct > 3) { cls = 'above'; arrow = '▲'; }
    else { cls = 'flat'; arrow = '≒'; }
    const signed = `${changePct >= 0 ? '+' : ''}${changePct.toFixed(1)}%`;
    return `<span class="comparison-change ${cls}">${arrow} ${signed}</span>`;
  }
  function comparisonPeriodLabel(label, years) {
    if (!label) return '長期比';
    if (label === '10年前比' || years == null) return label;
    const months = Math.max(0, Math.round(years * 12));
    const yearPart = Math.floor(months / 12);
    const monthPart = months % 12;
    const duration = `${yearPart > 0 ? `${yearPart}年` : ''}${monthPart > 0 ? `${monthPart}か月` : ''}` || '1か月未満';
    return `${label}（${duration}）`;
  }
  // 3年前比と長期比を併記。長期低迷の判定だけ別セクション振り分けに使う。
  function comparisonBlockHtml(code) {
    const change3y = _price3yChanges[code];
    const longChange = _longChanges[code];
    const longLabel = comparisonPeriodLabel(_longLabels[code], _longYears[code]);
    const slump = isLongSlump(code);
    const recoveryWatch = change3y != null && change3y < -3 && longChange != null && longChange >= -3;
    const blockCls = slump ? 'long-slump' : recoveryWatch ? 'recovery-watch' : '';
    const tag = slump ? '<span class="trend-tag">⚫ 長期低迷</span>'
      : recoveryWatch ? '<span class="trend-tag">🟣 長期成長の調整</span>' : '';
    const threeTip = _price3yRefs[code] != null
      ? `3年前20日平均 ${fmt(_price3yRefs[code])}円` : '再上場等により3年前比較不能';
    const longTip = _longRefs[code] != null
      ? `${longLabel}の基準20日平均 ${fmt(_longRefs[code])}円` : '長期比較不能';
    return `<div class="comparison-block ${blockCls}" title="${escapeHtml(`${threeTip} / ${longTip}`)}">
      <span class="comparison-metric"><span class="comparison-name">3年比</span> ${comparisonChangeHtml(change3y)}</span>
      <span class="comparison-sep">|</span>
      <span class="comparison-metric"><span class="comparison-name">${longLabel}</span> ${comparisonChangeHtml(longChange)}</span>
      ${tag}
    </div>`;
  }
  // 利回りは参考表示のみ。判定・フィルタ・並び順には使用しない。
  function yieldBlockHtml(s, price, divYield) {
    const hasBenefit = s.yutai_shares != null && s.yutai_shares > 0
      && s.yutai_value != null && s.yutai_value > 0;
    const benefitYield = hasBenefit && price != null && price > 0
      ? s.yutai_value / (price * s.yutai_shares) * 100 : null;
    const hasDividend = divYield != null && divYield >= 0;
    const totalYield = benefitYield != null && hasDividend ? benefitYield + divYield : null;
    const benefitText = benefitYield != null ? `${benefitYield.toFixed(2)}%` : '—（未入力）';
    const dividendText = hasDividend ? `${divYield.toFixed(2)}%` : '—';
    const totalText = totalYield != null ? `${totalYield.toFixed(2)}%` : '—';
    let cls = 'yield-block';
    if (totalYield != null && totalYield >= 5) cls += ' high';
    else if (totalYield != null && totalYield >= 3) cls += ' mid';
    const benefitDetail = hasBenefit
      ? `<div style="font-size:0.78em;color:#777">${s.yutai_shares}株で年${s.yutai_value.toLocaleString('ja-JP')}円相当${s.yutai_item ? `（${escapeHtml(s.yutai_item)}）` : ''}</div>`
      : '';
    return `<div class="${cls}">
      <div style="font-size:0.78em;color:#777">参考利回り（判定・並び順には不使用）</div>
      <div><strong>合計 <span class="ty-num">${totalText}</span></strong>
        <span style="font-size:0.84em;color:#666">＝ 優待 ${benefitText} ＋ 配当 ${dividendText}</span></div>
      ${benefitDetail}
    </div>`;
  }
  // 3指標を色付きチップグリッドで表示(0=緑/-1=黄/-2=赤)
  function valuationGridHtml(items) {
    return items.map(it => {
      let cls;
      if      (it.score === 0)  cls = 'ic-good-strong';   // ペナルティなし = 緑
      else if (it.score === -1) cls = 'ic-bad';            // 軽い減点 = 薄赤
      else                      cls = 'ic-bad-strong';     // 重い減点 = 濃赤
      const display = it.score === 0 ? '✓' : `${it.score}`;
      const bbZHtml = it.label === 'BB' && it.signedZ
        ? `<div class="bb-z">現在 ${it.signedZ}</div>` : '';
      const mainBlock = `<div class="ic ${cls}" title="${escapeHtml(it.full)}">
        <div class="ic-name">${it.label}</div>
        <div class="ic-val">${it.value}</div>
        ${bbZHtml}
        <div class="ic-pts">${display}</div>
      </div>`;
      if (it.label !== 'RSI週' || it.rsiSma14 == null) return mainBlock;
      const smaTip = `RSI ${it.value} / 14SMA ${it.rsiSma14.toFixed(1)} → RSIは${it.relationLabel}`;
      const smaBlock = `<div class="ic ic-rsi-sma ${it.relation}" title="${escapeHtml(smaTip)}">
        <div class="ic-name">RSI 14SMA</div>
        <div class="ic-val">${it.rsiSma14.toFixed(1)}</div>
        <div class="ic-pts">RSI ${it.relationLabel}</div>
      </div>`;
      return mainBlock + smaBlock;
    }).join('');
  }

  function render() {
    const grid = document.getElementById('grid');
    if (STOCKS.length === 0) {
      grid.innerHTML = '<div class="empty">押し目買いリストに反映中の銘柄がありません。</div>';
      return;
    }
    // 各銘柄を同じ3指標で判定し、52週レンジ位置を計算
    const enrichedAll = STOCKS.map(s => {
      const p = _prices[s.code];
      const high52 = _high52s[s.code], low52 = _low52s[s.code];
      const rangePos = (high52 != null && low52 != null && high52 > low52 && p != null)
        ? (p - low52) / (high52 - low52) * 100 : null;
      const val = valuationLevel(p, high52, low52, _rsis[s.code], _rsiSma14s[s.code], _bbUppers[s.code], _bbLowers[s.code]);
      return {
        s, p, valScore: val.score, valCls: val.cls, valLabel: val.label, rangePos,
        longSlump: isLongSlump(s.code), longChange: _longChanges[s.code],
      };
    });

    // 52週レンジ位置フィルタを適用
    const fRangePosMax = parseFloat(document.getElementById('f-rangepos-max').value);
    let hidden = 0;
    const enriched = enrichedAll.filter(e => {
      if (!isNaN(fRangePosMax) && e.rangePos != null && e.rangePos > fRangePosMax) {
        hidden++; return false;
      }
      return true;
    });

    // 長期低迷はテクニカル判定に関係なく、常に最下部の専用枠へ送る。
    const groups = { cheap: [], neutral: [], expensive: [], longSlump: [] };
    for (const e of enriched) {
      if (e.longSlump) groups.longSlump.push(e);
      else groups[e.valCls].push(e);
    }
    // 同スコアなら、投資方針に合わせて52週レンジ位置が低い順
    for (const k of Object.keys(groups)) {
      groups[k].sort((a, b) =>
        b.valScore - a.valScore || (a.rangePos ?? Infinity) - (b.rangePos ?? Infinity)
      );
    }
    groups.longSlump.sort((a, b) =>
      (a.longChange ?? Infinity) - (b.longChange ?? Infinity)
      || (a.rangePos ?? Infinity) - (b.rangePos ?? Infinity)
    );

    grid.innerHTML = '';

    const sectionMeta = [
      { key: 'cheap',     icon: '🟢', label: '押し目候補', desc: '3指標で過熱サインなし。52週レンジ位置が低い順' },
      { key: 'neutral',   icon: '🟡', label: '反発・注意', desc: '52週レンジ・週足RSI・BBのどれかに軽い過熱サイン' },
      { key: 'expensive', icon: '🔴', label: '過熱気味', desc: '3指標に複数または強い過熱サイン' },
      { key: 'longSlump', icon: '⚫', label: '長期低迷（別枠）', desc: '3年比と長期比がともに−3%未満。フィルター通過時も常に最下部へ分離' },
    ];

    sectionMeta.forEach(meta => {
      const list = groups[meta.key];
      if (list.length === 0) return;
      const sec = document.createElement('div');
      sec.className = 'section-header';
      sec.innerHTML = `<h2>${meta.icon} ${meta.label} <span class="count">(${list.length}件)</span></h2><p class="section-desc">${meta.desc}</p>`;
      grid.appendChild(sec);
      const subgrid = document.createElement('div');
      subgrid.className = 'subgrid';
      grid.appendChild(subgrid);

      list.forEach(({ s, p, longSlump }) => {
        const badgeMkt = s.market === 'プライム' ? 'badge-prime' : 'badge-standard';
        const rsi = _rsis[s.code], rsiSma14 = _rsiSma14s[s.code];
        const card = document.createElement('div');
        card.className = 'card';
        card.innerHTML = `
          <div class="card-head">
            <span class="code">${s.code}</span>
            <span class="badge ${badgeMkt}">${s.market}</span>
            ${longSlump
              ? '<span class="vbadge long-slump" title="3年比と長期比がともに−3%未満">⚫ 長期低迷</span>'
              : valuationBadge(p, _high52s[s.code], _low52s[s.code], rsi, rsiSma14, _bbUppers[s.code], _bbLowers[s.code])}
            <span class="name">${escapeHtml(s.name)}</span>
            <button class="star-btn" data-code="${s.code}" title="★解除">★</button>
          </div>
          <div class="price-row">
            <span><span class="label">現在</span> <strong>${p != null ? fmt(p) : '—'} 円</strong></span>
            ${(() => {
              const h = _high52s[s.code], l = _low52s[s.code];
              if (h == null || p == null) return '';
              const downHigh = ((p - h) / h) * 100;
              const upLow = (l != null) ? ((p - l) / l) * 100 : null;
              const downCls = downHigh <= -20 ? 'high-down-large' : downHigh <= -10 ? 'high-down-med' : '';
              return `<span style="font-size:0.82em;color:#666">|</span>
                <span style="font-size:0.85em">52週高値 ${fmt(h)}円
                  <strong class="${downCls}">${downHigh.toFixed(1)}%</strong></span>
                ${upLow != null ? `<span style="font-size:0.78em;color:#888">/ 安値 ${fmt(l)}円 +${upLow.toFixed(1)}%</span>` : ''}`;
            })()}
          </div>
          <div class="ic-grid">${valuationGridHtml(valuationLevel(p, _high52s[s.code], _low52s[s.code], rsi, rsiSma14, _bbUppers[s.code], _bbLowers[s.code]).items)}</div>
          ${comparisonBlockHtml(s.code)}
          ${yieldBlockHtml(s, p, _divYields[s.code])}
          <div class="actions">
            <a href="https://finance.yahoo.co.jp/quote/${s.code}.T" target="_blank" rel="noopener">Y!</a>
            <a href="https://www.google.com/search?q=${encodeURIComponent(s.code + ' ' + s.name + ' 株主優待')}" target="_blank" rel="noopener">検索</a>
            <button class="edit-btn" data-code="${s.code}">編集</button>
          </div>
        `;
        subgrid.appendChild(card);
      });
    });
    const filterNote = hidden > 0
      ? ` <span style="color:#d35400">(条件で <strong>${hidden}</strong> 件非表示)</span>` : '';
    document.getElementById('summary').innerHTML =
      `<strong>${enriched.length} / ${STOCKS.length} 銘柄表示中</strong>${filterNote}:
      🟢 押し目候補 <strong style="color:#27ae60">${groups.cheap.length}</strong> /
      🟡 反発・注意 <strong style="color:#7f8c8d">${groups.neutral.length}</strong> /
      🔴 過熱気味 <strong style="color:#c0392b">${groups.expensive.length}</strong> /
      ⚫ 長期低迷 <strong style="color:#34495e">${groups.longSlump.length}</strong><br>
      <span style="font-size:0.85em;color:#666">通常枠: 過熱サインが少ない順 → 52週レンジ位置が低い順。長期低迷は常に最下部へ分離</span><br>
      <span style="font-size:0.82em;color:#666">
        RSI: <span style="background:#e67e22;color:#fff;padding:1px 4px;border-radius:3px">▲ 上</span> 14SMAより上 /
        <span style="background:#2980b9;color:#fff;padding:1px 4px;border-radius:3px">▼ 下</span> 14SMAより下（判定外）。
        BB: 現在値のσ区間と正確な位置を表示（+1σ以上は過熱減点）
      </span>`;
  }

  async function refreshPrices() {
    const btn = document.getElementById('refresh');
    const status = document.getElementById('status');
    btn.disabled = true;
    status.textContent = '取得中…';
    try {
      const res = await fetch('/api/prices');
      const data = await res.json();
      _prices = data.prices || {};
      _rsis = data.rsis || {};
      _rsiSma14s = data.rsi_sma14s || {};
      _high52s = data.high52s || {};
      _low52s = data.low52s || {};
      _bbUppers = data.bb_uppers || {};
      _bbLowers = data.bb_lowers || {};
      _price3yRefs = data.price3y_refs || {};
      _price3yChanges = data.price3y_changes || {};
      _longRefs = data.long_refs || {};
      _longChanges = data.long_changes || {};
      _longLabels = data.long_labels || {};
      _longYears = data.long_years || {};
      _divYields = data.div_yields || {};
      render();
      status.textContent = `最終更新: ${data.fetched_at}`;
    } catch (e) {
      status.textContent = '取得失敗: ' + e.message;
    } finally {
      btn.disabled = false;
    }
  }

  // ===== モーダル & ボタンハンドラ =====
  let _editCode = null;
  function openModal(code) {
    const s = STOCKS.find(x => x.code === code);
    if (!s) return;
    _editCode = code;
    document.getElementById('m-title').textContent = `${code} ${s.name} を編集`;
    document.getElementById('m-judgment').value = s.judgment || '';
    document.getElementById('m-analysis').value = s.analysis || '';
    document.getElementById('m-yutai-shares').value = s.yutai_shares != null ? s.yutai_shares : '';
    document.getElementById('m-yutai-value').value = s.yutai_value != null ? s.yutai_value : '';
    document.getElementById('m-yutai-item').value = s.yutai_item || '';
    document.getElementById('analysis-modal').classList.add('show');
  }
  function closeModal() {
    document.getElementById('analysis-modal').classList.remove('show');
    _editCode = null;
  }
  async function saveAnalysis() {
    if (!_editCode) return;
    const judgment = document.getElementById('m-judgment').value || null;
    const a = document.getElementById('m-analysis').value.trim() || null;
    const ys = document.getElementById('m-yutai-shares').value.trim();
    const yv = document.getElementById('m-yutai-value').value.trim();
    const yi = document.getElementById('m-yutai-item').value.trim() || null;
    try {
      const res = await fetch('/api/analysis/' + _editCode, {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({
          judgment, target_price: null, analysis: a,
          yutai_shares: ys === '' ? null : parseFloat(ys),
          yutai_value: yv === '' ? null : parseFloat(yv),
          yutai_item: yi,
        })
      });
      const data = await res.json();
      const s = STOCKS.find(x => x.code === _editCode);
      if (s) {
        s.judgment = data.judgment;
        s.target_price = data.target_price;
        s.analysis = data.analysis;
        s.yutai_shares = data.yutai_shares;
        s.yutai_value = data.yutai_value;
        s.yutai_item = data.yutai_item;
      }
      render();
      closeModal();
    } catch (e) { alert('保存失敗: ' + e.message); }
  }
  async function unstar(code) {
    if (!confirm('この銘柄を★解除してウォッチリストから外しますか?')) return;
    try {
      await fetch('/api/star/' + code, { method: 'POST' });
      const idx = STOCKS.findIndex(s => s.code === code);
      if (idx >= 0) STOCKS.splice(idx, 1);
      renderStockList();
      render();
    } catch (e) { alert('解除失敗: ' + e.message); }
  }

  document.getElementById('m-cancel').addEventListener('click', closeModal);
  document.getElementById('m-save').addEventListener('click', saveAnalysis);
  document.getElementById('analysis-modal').addEventListener('click', e => {
    if (e.target.id === 'analysis-modal') closeModal();
  });
  document.getElementById('grid').addEventListener('click', e => {
    const editBtn = e.target.closest('.edit-btn');
    if (editBtn) { openModal(editBtn.dataset.code); return; }
    const star = e.target.closest('.star-btn');
    if (star) { unstar(star.dataset.code); return; }
  });
  document.getElementById('refresh').addEventListener('click', refreshPrices);
  document.querySelectorAll('.view-tab').forEach(tab => {
    tab.addEventListener('click', () => switchView(tab.dataset.view));
  });

  // フィルタ入力イベント (rerenderのみ、API再取得不要)
  ['f-rangepos-max'].forEach(id => {
    document.getElementById(id).addEventListener('input', render);
  });
  document.getElementById('f-clear').addEventListener('click', () => {
    document.getElementById('f-rangepos-max').value = '';
    render();
  });

  renderStockList();
  refreshPrices();
</script>

</body>
</html>
"""


if __name__ == "__main__":
    # 0.0.0.0 で全インターフェースにバインド → 同じWiFiのスマホ等からアクセス可能
    app.run(host="0.0.0.0", port=5001, debug=False)
