// The generator and visible watchlist use the same eligibility function.
const {passesWatchlist, validPeriod, EXCLUSION_GAP_PP} = require('./comparison.js');
function buildSnapshot(stocks, data, generatedAt) {
  const dates = [];
  const universe = stocks.map(stock => {
    const code = stock.code;
    const price = data.prices[code], high = data.high52s[code], low = data.low52s[code];
    const validPrice = [price,high,low].every(Number.isFinite) && price > 0 && high >= low;
    const rangePos = validPrice && high > low ? (price-low)/(high-low)*100 : null;
    const periods = data.topix_comparisons[code];
    const coverage = [3,5,10].filter(y => validPeriod(periods?.[y]));
    for (const y of coverage) if (periods[y].end) dates.push(periods[y].end);
    return {code, name:stock.name, eligible:passesWatchlist({rangePos,periods}), coverage, validPrice};
  }).sort((a,b)=>a.code.localeCompare(b.code));
  return {schema:1, generatedAt, marketDate:dates.sort().at(-1) || null,
    rules:{rangeMax:30, gap:EXCLUSION_GAP_PP, periods:[3,5,10], rule:'latest'},
    ready:universe.length>0 && universe.every(s=>s.validPrice) && dates.length>0, universe};
}
module.exports = {buildSnapshot};
if (require.main === module) {
  const input = JSON.parse(require('node:fs').readFileSync(0,'utf8'));
  process.stdout.write(JSON.stringify(buildSnapshot(input.stocks,input.data,input.generatedAt)));
}
