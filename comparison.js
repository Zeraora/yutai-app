// 純粋な計算関数。ブラウザーとNodeのテストで同じ実装を使う。
const ComparisonMath = (() => {
  const EXCLUSION_GAP_PP = 5;
  function validPeriod(p) {
    return Boolean(p?.available && ['stock_return', 'benchmark_return', 'excess_pp', 'stock_cagr', 'benchmark_cagr', 'excess_cagr_pp'].every(key => Number.isFinite(p[key])));
  }
  function matches(p, filter) {
    if (filter === 'all') return true;
    if (filter === 'missing') return !validPeriod(p);
    if (!validPeriod(p)) return false;
    if (filter === 'win') return p.excess_cagr_pp > 1e-9;
    if (filter === 'lose') return p.excess_cagr_pp < -1e-9;
    if (filter === 'near') return Math.abs(p.excess_cagr_pp) <= 1;
    if (filter === 'rolling') return p.rolling?.complete === true && p.rolling.count === 36 && p.rolling.win_rate >= 70;
    return false;
  }
  // 負けが揃う銘柄だけを非表示にする。履歴不足は負けとして扱わない。
  function underperformance(periods, rule = 'latest') {
    if (!['latest', 'rolling'].includes(rule)) return 'insufficient';
    const selected = [3, 5, 10].map(year => periods?.[String(year)]);
    if (!selected.every(validPeriod)) return 'insufficient';
    if (rule === 'rolling' && !selected.every(p => p.rolling?.complete === true
        && p.rolling.count === 36 && Number.isFinite(p.rolling.max_gap))) return 'insufficient';
    const below = selected.every(p => p.excess_cagr_pp <= -EXCLUSION_GAP_PP);
    return below && (rule === 'latest' || selected.every(p => p.rolling.max_gap <= -EXCLUSION_GAP_PP))
      ? 'exclude' : 'keep';
  }
  function passesWatchlist({rangePos, periods}, {rangeMax = 30, hide = true, rule = 'latest'} = {}) {
    if (hide && underperformance(periods, rule) === 'exclude') return false;
    return !Number.isFinite(rangeMax) || rangePos == null || rangePos <= rangeMax;
  }
  function benefitScenario({ price, shares, annualValue, years, stockCagr, benchmarkCagr }) {
    if (![price, shares, annualValue, years, stockCagr, benchmarkCagr].every(Number.isFinite)
        || price <= 0 || shares <= 0 || !Number.isInteger(shares) || annualValue < 0
        || ![3, 5, 10].includes(years) || stockCagr <= -100 || benchmarkCagr <= -100) return null;
    const initial = price * shares;
    const stockEnd = initial * (1 + stockCagr / 100) ** years;
    const benchmarkEnd = initial * (1 + benchmarkCagr / 100) ** years;
    // 優待は年末に同額を受け取り、再投資せず額面を積み上げる仮定。
    const benefitTotal = annualValue * years;
    const totalEnd = stockEnd + benefitTotal;
    const annualNeeded = Math.max(0, (benchmarkEnd - stockEnd) / years);
    const annualizedWithBenefits = ((totalEnd / initial) ** (1 / years) - 1) * 100;
    const difference = totalEnd - benchmarkEnd;
    const result = {initial, stockEnd, benchmarkEnd, benefitTotal, totalEnd, annualNeeded, annualizedWithBenefits, difference};
    if (!Object.values(result).every(Number.isFinite)) return null;
    return {...result, covers: difference >= -initial * 1e-10};
  }
  return {EXCLUSION_GAP_PP, validPeriod, matches, underperformance, passesWatchlist, benefitScenario};
})();
if (typeof module !== 'undefined') module.exports = ComparisonMath;
