"""
静的HTML生成スクリプト(GitHub Actions等から定期実行)。

★銘柄のみを対象に yfinance から最新データを取得し、
WATCHLIST_HTML をベースに「データ埋め込み済み・編集機能なし」の
完全自己完結HTMLを docs/index.html へ書き出す。

ローカル動作確認:
    cd /Users/tatsuki/Documents/yutai-app
    python3 generator.py
"""
from __future__ import annotations

import json
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

import app
from app import (
    load_stocks,
    fetch_prices_and_rsi,
    fetch_metrics,
    WATCHLIST_HTML,
)


# 静的化用の上書きスクリプト+CSS
# - fetch('/api/prices') を埋め込みデータでハイジャック
# - 他の /api/ POSTは alert で拒否
# - 編集系 UI(★解除/編集/モーダル等)を CSS で非表示
STATIC_OVERRIDE_TEMPLATE = """<script>
const EMBEDDED_API_DATA = __EMBEDDED__;
const _origFetch = window.fetch;
window.fetch = async (url, opts) => {
  if (typeof url === 'string' && (url === '/api/prices' || url.startsWith('/api/prices'))) {
    return new Response(JSON.stringify(EMBEDDED_API_DATA), {
      status: 200, headers: {'Content-Type': 'application/json'}
    });
  }
  if (typeof url === 'string' && url.startsWith('/api/')) {
    alert('静的サイトのため編集機能は利用できません。Macアプリ側で操作してください。');
    return new Response('{}', {status: 405});
  }
  return _origFetch(url, opts);
};
</script>
<style>
/* 静的デプロイ: 編集系UIを隠す */
.star-btn, .edit-btn { display: none !important; }
#refresh { display: none !important; }
#status { color: #2980b9; font-weight: bold; }
.topbar::after {
  content: '🔒 静的版(編集はMacで)';
  font-size: 0.78em; color: #888; margin-left: 1em;
}
</style>
"""


def main() -> None:
    # ★銘柄だけ抽出 → モジュール変数を差し替えて fetch を高速化
    all_stocks = load_stocks()
    starred = [s for s in all_stocks if s.get("starred")]
    print(f"★銘柄: {len(starred)} 件")
    if not starred:
        print("⚠ ★銘柄ゼロ — 出力中止")
        return
    app.STOCKS = starred

    # データ取得
    print("yfinance データ取得中… (1〜3分)")
    prices, rsis, sma200s, rsi30s, high52s, low52s, bb_uppers, bb_lowers = fetch_prices_and_rsi()
    metrics = fetch_metrics()
    ok_p = sum(1 for v in prices.values() if v)
    print(f"  株価取得: {ok_p}/{len(starred)} 件成功")

    embedded = {
        "prices": prices,
        "roes": {c: m["roe"] for c, m in metrics.items()},
        "pers": {c: m["per"] for c, m in metrics.items()},
        "avg_roes": {c: m["avg_roe"] for c, m in metrics.items()},
        "pbrs": {c: m["pbr"] for c, m in metrics.items()},
        "div_yields": {c: m["div_yield"] for c, m in metrics.items()},
        "pegs": {c: m["peg"] for c, m in metrics.items()},
        "earnings_growths": {c: m["earnings_growth"] for c, m in metrics.items()},
        "payout_ratios": {c: m["payout_ratio"] for c, m in metrics.items()},
        "equity_ratios": {c: m["equity_ratio"] for c, m in metrics.items()},
        "rsis": rsis,
        "sma200s": sma200s,
        "rsi30_prices": rsi30s,
        "high52s": high52s,
        "low52s": low52s,
        "bb_uppers": bb_uppers,
        "bb_lowers": bb_lowers,
        "verified": {s["code"]: s.get("last_verified") for s in starred},
        "fetched_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }

    # WATCHLIST_HTML の Jinja プレースホルダ置換
    stocks_json = json.dumps(starred, ensure_ascii=False)
    html = WATCHLIST_HTML.replace("{{ stocks_json | safe }}", stocks_json)

    # 静的化オーバーライドを最初の <script> 直前に挿入
    embedded_json = json.dumps(embedded, ensure_ascii=False)
    override = STATIC_OVERRIDE_TEMPLATE.replace("__EMBEDDED__", embedded_json)
    html = html.replace("<script>", override + "<script>", 1)

    # 出力
    out_dir = Path(__file__).parent / "docs"
    out_dir.mkdir(exist_ok=True)
    out_file = out_dir / "index.html"
    out_file.write_text(html, encoding="utf-8")
    print(f"✓ 生成完了: {out_file} ({len(html):,} bytes)")


if __name__ == "__main__":
    main()
