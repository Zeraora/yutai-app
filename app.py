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


def _compute_rsi_weekly(close, period: int = 14) -> float | None:
    """日足 Close を週足にリサンプルして Wilder RSI を計算。"""
    if close is None or len(close) < 5:
        return None
    # 各週の最終取引日の終値を週足とする
    weekly = close.resample("W").last().dropna()
    if len(weekly) < period + 1:
        return None
    delta = weekly.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    avg_gain = gain.ewm(alpha=1 / period, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1 / period, adjust=False).mean()
    last_loss = float(avg_loss.iloc[-1])
    last_gain = float(avg_gain.iloc[-1])
    if last_loss == 0:
        return 100.0
    rs = last_gain / last_loss
    return float(100 - (100 / (1 + rs)))


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
]:
    """価格 / 週足RSI / SMA200 / RSI30到達価格 / 52週高値 / 52週安値 / BB上限 / BB下限 を一括取得。"""
    symbols = [f"{s['code']}.T" for s in STOCKS]
    prices: dict[str, float | None] = {s["code"]: None for s in STOCKS}
    rsis: dict[str, float | None] = {s["code"]: None for s in STOCKS}
    sma200s: dict[str, float | None] = {s["code"]: None for s in STOCKS}
    rsi30s: dict[str, float | None] = {s["code"]: None for s in STOCKS}
    high52s: dict[str, float | None] = {s["code"]: None for s in STOCKS}
    low52s: dict[str, float | None] = {s["code"]: None for s in STOCKS}
    bb_uppers: dict[str, float | None] = {s["code"]: None for s in STOCKS}
    bb_lowers: dict[str, float | None] = {s["code"]: None for s in STOCKS}

    def _populate(code: str, close):
        if close is None or len(close) == 0: return
        prices[code] = float(close.iloc[-1])
        rsis[code] = _compute_rsi_weekly(close)
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

    try:
        data = yf.download(
            symbols, period="1y", progress=False,
            group_by="ticker", auto_adjust=True, threads=True,
        )
    except Exception as e:
        app.logger.warning(f"bulk download failed: {e}")
        data = None

    if data is not None:
        for s in STOCKS:
            sym = f"{s['code']}.T"
            try:
                close = data[sym]["Close"].dropna()
                _populate(s["code"], close)
            except (KeyError, ValueError, AttributeError):
                pass

    missing = [code for code, price in prices.items() if price is None]
    for code in missing:
        try:
            t = yf.Ticker(f"{code}.T")
            hist = t.history(period="1y")
            if not hist.empty:
                _populate(code, hist["Close"])
        except Exception as e:
            app.logger.warning(f"individual fetch failed for {code}: {e}")

    return prices, rsis, sma200s, rsi30s, high52s, low52s, bb_uppers, bb_lowers


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


@app.route("/api/prices")
def api_prices():
    prices, rsis, sma200s, rsi30s, high52s, low52s, bb_uppers, bb_lowers = fetch_prices_and_rsi()
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
        "sma200s": sma200s,
        "rsi30_prices": rsi30s,
        "high52s": high52s,
        "low52s": low52s,
        "bb_uppers": bb_uppers,
        "bb_lowers": bb_lowers,
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
</style>
</head>
<body>

<h1>★ 押し目買いウォッチリスト <a href="/">← 全銘柄一覧へ</a></h1>

<div class="topbar">
  <button id="refresh">価格を更新</button>
  <span id="status">起動時に自動取得します…</span>
  <span style="margin-left:auto"></span>
  <label style="font-size:0.9em">
    優待取得必要額 ≤ <input type="number" id="f-cost-max" value="50" step="10" min="0" style="width:80px;padding:0.25em 0.4em;border:1px solid #bbb;border-radius:3px"> 万円
  </label>
  <label style="font-size:0.9em">
    総合利回り ≥ <input type="number" id="f-yield-min" value="3" step="0.5" min="0" style="width:60px;padding:0.25em 0.4em;border:1px solid #bbb;border-radius:3px"> %
  </label>
  <label style="font-size:0.9em">
    52週レンジ位置 ≤ <input type="number" id="f-rangepos-max" value="40" step="5" min="0" max="100" style="width:60px;padding:0.25em 0.4em;border:1px solid #bbb;border-radius:3px"> %
  </label>
  <button id="f-clear" style="padding:0.35em 0.8em;background:#95a5a6;color:#fff;border:none;border-radius:3px;cursor:pointer;font-size:0.85em">条件クリア</button>
</div>

<div class="summary" id="summary">
  <strong>判定方式: 11指標減点法 + ⭐優良マーク</strong>(<code>0</code>が満点、減点が少ないほど候補)<br>
  <span style="font-size:0.85em">
  価格系4(52週・SMA・RSI週・BB)+ バリュー3(PER・PBR・PEG)+ 配当2(配当・配当性向)+ 質2(成長・ROE質)。総合 <code>0</code>〜<code>-22</code>。<br>
  🟢 <strong>割安圏</strong>(≥-4)/ 🟡 <strong>適正圏</strong>(-5〜-12)/ 🔴 <strong>割高圏</strong>(≤-13)<br>
  <strong>⭐</strong> = 減点ゼロ + ROE平均 ≥ 12%(真の優良) /
  <strong>☆</strong> = 減点≤2 + ROE平均≥12% + 業績成長プラス(準優良)<br>
  チップ色: <span style="background:#27ae60;color:#fff;padding:1px 6px;border-radius:3px">✓ 0</span> = ペナルティなし /
  <span style="background:#fadbd8;color:#922b21;padding:1px 6px;border-radius:3px">-1</span> = 軽い減点 /
  <span style="background:#c0392b;color:#fff;padding:1px 6px;border-radius:3px">-2</span> = 重い減点
  </span>
</div>

<div id="grid" class="grid"></div>

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
  let _prices = {}, _pers = {}, _roes = {}, _avgRoes = {}, _rsis = {};
  let _sma200s = {}, _rsi30s = {};
  let _high52s = {}, _low52s = {};
  let _bbUppers = {}, _bbLowers = {}, _pbrs = {}, _divYields = {}, _pegs = {};
  let _earningsGrowths = {}, _payoutRatios = {}, _equityRatios = {};

  function escapeHtml(s) {
    if (s == null) return '';
    return String(s).replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  }
  function judgmentBadge(j) {
    const labels = { buy: '🟢買い', hold: '🟡中立', avoid: '🔴見送り' };
    if (!j) return '<span class="jbadge none">判定なし</span>';
    return `<span class="jbadge ${j}">${labels[j] || j}</span>`;
  }
  function fmt(n, suf='') { return n == null ? '—' : Math.round(n).toLocaleString('ja-JP') + suf; }

  // ===== 割安度判定 (11指標、減点法) =====
  // 「割高 / 過熱 / リスク」サインがあれば減点(-1 または -2)、それ以外は 0。
  // 最大は 0(全指標でペナルティなし)、最低は -22。減点が少ないほど候補。
  function valuationLevel(price, high52, low52, sma200, rsi, per, pbr, divYield, peg, bbUpper, bbLower, avgRoe, roe, earningsGrowth, payoutRatio) {
    const items = [];
    const push = (score, label, value, full) => items.push({ score, label, value, full });
    // 1. 52週レンジ位置(緩和: 80%以上だけ減点)
    if (high52 != null && low52 != null && high52 > low52 && price != null) {
      const pos = (price - low52) / (high52 - low52);
      const pct = (pos * 100).toFixed(0);
      const v = `${pct}%`;
      if (pos >= 0.80) push(-2, '52週', v, `52週レンジ ${pct}% (高値圏 ≥80%)`);
      else             push( 0, '52週', v, `52週レンジ ${pct}% (ペナルティなし)`);
    } else push(0, '52週', '—', '52週レンジ計算不能');
    // 2. SMA200乖離: 価格がSMA200より上なら減点
    if (sma200 != null && price != null) {
      const dev = (price / sma200 - 1) * 100;
      const v = `${dev >= 0 ? '+' : ''}${dev.toFixed(1)}%`;
      if      (dev >= +5) push(-2, 'SMA', v, `SMA200比 ${dev.toFixed(1)}% (大きく上 +5%以上)`);
      else if (dev >  +0.5) push(-1, 'SMA', v, `SMA200比 ${dev.toFixed(1)}% (上)`);
      else                  push( 0, 'SMA', v, `SMA200比 ${dev.toFixed(1)}% (下 or 接近、ペナルティなし)`);
    } else push(0, 'SMA', '—', 'SMA200なし');
    // 3. 週足RSI: 50以上で減点
    if (rsi == null) push(0, 'RSI週', '—', 'RSI週なし');
    else {
      const v = rsi.toFixed(1);
      if      (rsi >= 70) push(-2, 'RSI週', v, `RSI週 ${v} 買われ過ぎ`);
      else if (rsi >= 50) push(-1, 'RSI週', v, `RSI週 ${v} 強い`);
      else                push( 0, 'RSI週', v, `RSI週 ${v} (50未満、ペナルティなし)`);
    }
    // 4. ボリンジャーバンド位置: 上限近接以上で減点
    if (bbUpper == null || bbLower == null || price == null) {
      push(0, 'BB', '—', 'BBなし');
    } else if (price >= bbUpper) {
      push(-2, 'BB', '上限超', `BB上限超え ${Math.round(price)}≥${Math.round(bbUpper)}`);
    } else if (price >= bbUpper * 0.98) {
      push(-1, 'BB', '上限近', `BB上限近 ${Math.round(price)}`);
    } else {
      push( 0, 'BB', '安全', 'BB上限から離れている (ペナルティなし)');
    }
    // 5. PER絶対水準: 20以上で減点(赤字も減点)
    if (per == null || per <= 0) push(-2, 'PER', '—', 'PER赤字/取得不能(リスク)');
    else {
      const v = per.toFixed(1);
      if      (per >= 30) push(-2, 'PER', v, `PER ${v} 超割高(≥30)`);
      else if (per >= 20) push(-1, 'PER', v, `PER ${v} 高め(≥20)`);
      else                push( 0, 'PER', v, `PER ${v} (20未満、ペナルティなし)`);
    }
    // 6. PBR: 2以上で減点
    if (pbr == null) push(-1, 'PBR', '—', 'PBR取得不能(リスク)');
    else {
      const v = pbr.toFixed(2);
      if      (pbr >= 4) push(-2, 'PBR', v, `PBR ${v} 超高い(≥4)`);
      else if (pbr >= 2) push(-1, 'PBR', v, `PBR ${v} 高い(≥2)`);
      else                push( 0, 'PBR', v, `PBR ${v} (2未満、ペナルティなし)`);
    }
    // 7. 配当利回り: 1.5%未満で減点(0.5%未満は強い減点)
    if (divYield == null)        push(-1, '配当', '—', '配当データなし');
    else {
      const v = `${divYield.toFixed(2)}%`;
      if      (divYield < 0.5)  push(-2, '配当', v, `配当 ${v} 微配/無配`);
      else if (divYield < 1.5)  push(-1, '配当', v, `配当 ${v} 低い`);
      else                       push( 0, '配当', v, `配当 ${v} (1.5%以上、ペナルティなし)`);
    }
    // 8. PEG ratio(緩和: null/取得不能はペナルティなし)
    if (peg == null || peg <= 0) push(0, 'PEG', '—', 'PEGなし(緩和でペナルティなし)');
    else {
      const v = peg.toFixed(2);
      if      (peg >= 3) push(-2, 'PEG', v, `PEG ${v} 超高い(≥3)`);
      else if (peg >= 2) push(-1, 'PEG', v, `PEG ${v} 高い(≥2)`);
      else                push( 0, 'PEG', v, `PEG ${v} (2未満、ペナルティなし)`);
    }
    // 9. 業績成長 (earningsGrowth)
    if (earningsGrowth == null) push(0, '成長', '—', '業績成長データなし(ペナルティなし)');
    else {
      const v = `${(earningsGrowth*100).toFixed(1)}%`;
      if      (earningsGrowth <= -0.10) push(-2, '成長', v, `業績成長 ${v} 大幅減益(≤-10%)`);
      else if (earningsGrowth <  0)     push(-1, '成長', v, `業績成長 ${v} 減益`);
      else                              push( 0, '成長', v, `業績成長 ${v} (プラス、ペナルティなし)`);
    }
    // 10. 配当性向 (payoutRatio)
    if (payoutRatio == null) push(0, '配当性向', '—', '配当性向データなし');
    else {
      const v = `${(payoutRatio*100).toFixed(0)}%`;
      if      (payoutRatio > 1.0) push(-2, '配当性向', v, `配当性向 ${v} 赤字配当(無理な還元)`);
      else if (payoutRatio > 0.8) push(-1, '配当性向', v, `配当性向 ${v} 高すぎ(余力少)`);
      else                         push( 0, '配当性向', v, `配当性向 ${v} (持続性OK)`);
    }
    // 11. ROE劣化 (avg_roe + roe)
    {
      let pen = 0;
      let detail = '';
      let valDisp = '';
      if (avgRoe == null) {
        pen = -1; detail = 'ROE平均なし(質判定不能)'; valDisp = '—';
      } else {
        const apct = avgRoe * 100;
        valDisp = `${apct.toFixed(1)}%`;
        if (apct < 5) { pen -= 1; detail += `ROE平均${apct.toFixed(1)}%低い `; }
        if (roe != null && avgRoe > 0 && roe < avgRoe / 2) {
          pen -= 1;
          detail += `現在ROE${(roe*100).toFixed(1)}%大幅劣化`;
        }
        if (pen === 0) detail = `ROE平均${apct.toFixed(1)}% (質OK)`;
      }
      pen = Math.max(-2, pen); // -2 でクリップ
      push(pen, 'ROE質', valDisp, detail);
    }

    const score = items.reduce((s, it) => s + it.score, 0);
    // 減点法: 0が最良、減点が少ないほど良い
    let label, cls;
    if (score >= -4)       { label = '🟢 割安圏'; cls = 'cheap'; }
    else if (score <= -13) { label = '🔴 割高圏'; cls = 'expensive'; }
    else                   { label = '🟡 適正圏'; cls = 'neutral'; }
    return { label, cls, score, items };
  }
  // ⭐優良マーク: 減点法+質判定のハイブリッド
  function premiumMark(score, avgRoe, earningsGrowth) {
    if (score === 0 && avgRoe != null && avgRoe >= 0.12) return '⭐';
    if (score >= -2 && avgRoe != null && avgRoe >= 0.12
        && earningsGrowth != null && earningsGrowth >= 0) return '☆';
    return '';
  }
  function valuationBadge(price, high52, low52, sma200, rsi, per, pbr, divYield, peg, bbUpper, bbLower, avgRoe, roe, earningsGrowth, payoutRatio) {
    const r = valuationLevel(price, high52, low52, sma200, rsi, per, pbr, divYield, peg, bbUpper, bbLower, avgRoe, roe, earningsGrowth, payoutRatio);
    const tip = r.items.map(it => `${it.full} (${it.score})`).join('\n') + `\n= 合計 ${r.score}(0が満点、減点が少ないほど良い)`;
    const mark = premiumMark(r.score, avgRoe, earningsGrowth);
    const markHtml = mark ? ` <span class="premium" title="${mark === '⭐' ? '優良(減点ゼロ+ROE平均≥12%)' : '準優良(減点≤2+ROE平均≥12%+成長プラス)'}">${mark}</span>` : '';
    return `<span class="vbadge ${r.cls}" title="${escapeHtml(tip)}">${r.label} <small>${r.score}</small></span>${markHtml}`;
  }
  // 優待+配当の総合利回り表示
  function yieldBlockHtml(s, price, divYield) {
    const ys = s.yutai_shares, yv = s.yutai_value, yi = s.yutai_item;
    const hasY = (ys != null && yv != null && ys > 0 && yv > 0);
    if (!hasY && (divYield == null || divYield <= 0)) {
      return '<div class="yield-block placeholder">優待詳細未入力 — 編集ボタンから入力で総合利回り表示</div>';
    }
    let yutaiYield = null, totalCost = null;
    if (hasY && price != null && price > 0) {
      totalCost = price * ys;
      yutaiYield = (yv / totalCost) * 100;
    }
    const dy = (divYield != null && divYield > 0) ? divYield : 0;
    const ty = (yutaiYield != null ? yutaiYield : 0) + dy;
    const ytStr = yutaiYield != null ? yutaiYield.toFixed(2) + '%' : '—';
    const dyStr = (divYield != null && divYield > 0) ? divYield.toFixed(2) + '%' : '—';
    const tyStr = (ty > 0) ? ty.toFixed(2) + '%' : '—';
    const totalCostStr = totalCost != null ? Math.round(totalCost).toLocaleString('ja-JP') + '円' : '—';
    let yutaiLine = '';
    if (hasY) {
      yutaiLine = `<div style="font-size:0.85em">優待: <strong>${ys}株</strong>あたり <strong>${yv.toLocaleString('ja-JP')}円</strong>分の <strong>${escapeHtml(yi || '')}</strong> <span style="color:#888">(必要 ${totalCostStr})</span></div>`;
    }
    let cls = 'yield-block';
    if (ty >= 5) cls += ' high';
    else if (ty >= 3) cls += ' mid';
    return `<div class="${cls}">
      ${yutaiLine}
      <div style="font-weight:bold;font-size:1em">総合利回り <span class="ty-num">${tyStr}</span>
        <span style="font-weight:normal;font-size:0.82em;color:#666">= 優待 ${ytStr} + 配当 ${dyStr}</span>
      </div>
    </div>`;
  }
  // 8指標を色付きチップグリッドで表示(0=緑/-1=黄/-2=赤)
  function valuationGridHtml(items) {
    return items.map(it => {
      let cls;
      if      (it.score === 0)  cls = 'ic-good-strong';   // ペナルティなし = 緑
      else if (it.score === -1) cls = 'ic-bad';            // 軽い減点 = 薄赤
      else                      cls = 'ic-bad-strong';     // 重い減点 = 濃赤
      const display = it.score === 0 ? '✓' : `${it.score}`;
      return `<div class="ic ${cls}" title="${escapeHtml(it.full)}">
        <div class="ic-name">${it.label}</div>
        <div class="ic-val">${it.value}</div>
        <div class="ic-pts">${display}</div>
      </div>`;
    }).join('');
  }

  // ===== ルールベース自動判定 (廃止予定 - 互換維持のため残す) =====
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
    if (s.judgment) {
      return judgmentBadge(s.judgment) + ' <small style="color:#888">(手動)</small>';
    }
    const r = computeJudgment(per, roe, avgRoe, rsi, conf);
    const sign = r.score >= 0 ? '+' : '';
    const tip = r.items.map(([p, m]) => `${m} (${p >= 0 ? '+' : ''}${p})`).join(' / ') + ` = 合計${sign}${r.score}`;
    return `<span title="${escapeHtml(tip)}">${judgmentBadge(r.label)} <small>${sign}${r.score}</small></span>`;
  }

  function render() {
    const grid = document.getElementById('grid');
    if (STOCKS.length === 0) {
      grid.innerHTML = '<div class="empty">★を付けた銘柄がありません。<br><a href="/">全銘柄一覧</a> で気になる銘柄に★を付けてください。</div>';
      return;
    }
    // 各銘柄のスコア + 必要額・総合利回り計算
    const enrichedAll = STOCKS.map(s => {
      const p = _prices[s.code];
      const val = valuationLevel(p, _high52s[s.code], _low52s[s.code], _sma200s[s.code], _rsis[s.code], _pers[s.code], _pbrs[s.code], _divYields[s.code], _pegs[s.code], _bbUppers[s.code], _bbLowers[s.code], _avgRoes[s.code], _roes[s.code], _earningsGrowths[s.code], _payoutRatios[s.code]);
      // 必要額: yutai_shares 入力済ならそれ × 価格、それ以外は min_shares × 価格
      const reqShares = (s.yutai_shares != null && s.yutai_shares > 0) ? s.yutai_shares : s.min_shares;
      const reqCost = (p != null && reqShares) ? p * reqShares : null;
      // 総合利回り: 優待入力済なら計算、配当だけでも合算
      let totalYield = null;
      const dy = (_divYields[s.code] != null && _divYields[s.code] > 0) ? _divYields[s.code] : 0;
      if (s.yutai_shares != null && s.yutai_value != null && p != null && p > 0) {
        const yy = (s.yutai_value / (p * s.yutai_shares)) * 100;
        totalYield = yy + dy;
      } else if (dy > 0) {
        totalYield = dy;
      }
      return { s, p, valScore: val.score, valCls: val.cls, valLabel: val.label, reqCost, totalYield };
    });

    // フィルタ適用
    const fCostMax = parseFloat(document.getElementById('f-cost-max').value);
    const fYieldMin = parseFloat(document.getElementById('f-yield-min').value);
    const fRangePosMax = parseFloat(document.getElementById('f-rangepos-max').value);
    let hidden = 0;
    const enriched = enrichedAll.filter(e => {
      if (!isNaN(fCostMax) && e.reqCost != null && e.reqCost > fCostMax * 10000) { hidden++; return false; }
      // 総合利回りフィルタ: 優待未入力(yutai_shares/value が null)の銘柄は除外しない
      const yutaiEntered = (e.s.yutai_shares != null && e.s.yutai_value != null);
      if (!isNaN(fYieldMin) && yutaiEntered && (e.totalYield == null || e.totalYield < fYieldMin)) {
        hidden++; return false;
      }
      // 52週レンジ位置フィルタ: データなし(high == low等)は除外しない
      if (!isNaN(fRangePosMax)) {
        const h = _high52s[e.s.code], l = _low52s[e.s.code];
        if (h != null && l != null && h > l && e.p != null) {
          const pos = (e.p - l) / (h - l) * 100;
          if (pos > fRangePosMax) { hidden++; return false; }
        }
      }
      return true;
    });

    // 割安度でグルーピング (cheap → neutral → expensive)
    const groups = { cheap: [], neutral: [], expensive: [] };
    for (const e of enriched) groups[e.valCls].push(e);
    // 各グループ内でスコア降順 (減点が少ないほど上)
    for (const k of Object.keys(groups)) groups[k].sort((a, b) => b.valScore - a.valScore);

    grid.innerHTML = '';

    const sectionMeta = [
      { key: 'cheap',     icon: '🟢', label: '割安圏', desc: '減点 4点以下(スコア ≥ -4)、欠点が少ない上位候補。⭐は減点ゼロ+ROE平均≥12%、☆は減点≤2+ROE平均≥12%+業績成長プラス' },
      { key: 'neutral',   icon: '🟡', label: '適正圏', desc: '減点 5〜12点(スコア -5 〜 -12)' },
      { key: 'expensive', icon: '🔴', label: '割高圏', desc: '減点 13点以上(スコア ≤ -13)、複数指標で割高シグナル' },
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

      list.forEach(({ s, p }) => {
        const badgeMkt = s.market === 'プライム' ? 'badge-prime' : 'badge-standard';
        const per = _pers[s.code], roe = _roes[s.code], avgRoe = _avgRoes[s.code], rsi = _rsis[s.code];
        const sma = _sma200s[s.code];
        const card = document.createElement('div');
        card.className = 'card';
        card.innerHTML = `
          <div class="card-head">
            <span class="code">${s.code}</span>
            <span class="badge ${badgeMkt}">${s.market}</span>
            ${valuationBadge(p, _high52s[s.code], _low52s[s.code], sma, rsi, _pers[s.code], _pbrs[s.code], _divYields[s.code], _pegs[s.code], _bbUppers[s.code], _bbLowers[s.code], _avgRoes[s.code], _roes[s.code], _earningsGrowths[s.code], _payoutRatios[s.code])}
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
          <div class="ic-grid">${valuationGridHtml(valuationLevel(p, _high52s[s.code], _low52s[s.code], sma, rsi, _pers[s.code], _pbrs[s.code], _divYields[s.code], _pegs[s.code], _bbUppers[s.code], _bbLowers[s.code], _avgRoes[s.code], _roes[s.code], _earningsGrowths[s.code], _payoutRatios[s.code]).items)}</div>
          <div class="metrics">
            <span>PER ${per && per > 0 ? per.toFixed(1) + '倍' : '—'}</span>
            <span>ROE ${roe != null ? (roe*100).toFixed(1) + '%' : '—'}</span>
            <span>ROE平均 ${avgRoe != null ? (avgRoe*100).toFixed(1) + '%' : '—'}</span>
            <span>RSI週 ${rsi != null ? rsi.toFixed(1) : '—'}</span>
            <span>自己資本比率 ${_equityRatios[s.code] != null ? (_equityRatios[s.code]*100).toFixed(0) + '%' : '—'}</span>
          </div>
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
      🟢 割安圏 <strong style="color:#27ae60">${groups.cheap.length}</strong> /
      🟡 適正圏 <strong style="color:#7f8c8d">${groups.neutral.length}</strong> /
      🔴 割高圏 <strong style="color:#c0392b">${groups.expensive.length}</strong><br>
      <span style="font-size:0.85em;color:#666">並び順: 割安スコアが高い順(減点が少ない=上位)</span>`;
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
      _pers = data.pers || {};
      _roes = data.roes || {};
      _avgRoes = data.avg_roes || {};
      _rsis = data.rsis || {};
      _sma200s = data.sma200s || {};
      _rsi30s = data.rsi30_prices || {};
      _high52s = data.high52s || {};
      _low52s = data.low52s || {};
      _bbUppers = data.bb_uppers || {};
      _bbLowers = data.bb_lowers || {};
      _pbrs = data.pbrs || {};
      _divYields = data.div_yields || {};
      _pegs = data.pegs || {};
      _earningsGrowths = data.earnings_growths || {};
      _payoutRatios = data.payout_ratios || {};
      _equityRatios = data.equity_ratios || {};
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

  // フィルタ入力イベント (rerenderのみ、API再取得不要)
  ['f-cost-max', 'f-yield-min', 'f-rangepos-max'].forEach(id => {
    document.getElementById(id).addEventListener('input', render);
  });
  document.getElementById('f-clear').addEventListener('click', () => {
    document.getElementById('f-cost-max').value = '';
    document.getElementById('f-yield-min').value = '';
    document.getElementById('f-rangepos-max').value = '';
    render();
  });

  refreshPrices();
</script>

</body>
</html>
"""


if __name__ == "__main__":
    # 0.0.0.0 で全インターフェースにバインド → 同じWiFiのスマホ等からアクセス可能
    app.run(host="0.0.0.0", port=5001, debug=False)
