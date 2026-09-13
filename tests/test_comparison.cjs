const {test} = require('node:test');
const assert = require('node:assert/strict');
const {matches, underperformance, benefitScenario} = require('../comparison.js');

const period = {available:true, stock_return:80, benchmark_return:50, excess_pp:30, stock_cagr:12, benchmark_cagr:10, excess_cagr_pp:2};
const scenario = {price:1000, shares:100, annualValue:15000, years:5, stockCagr:0, benchmarkCagr:10};

test('benefit threshold respects compounding and adds unreinvested benefits in yen', () => {
  const r = benefitScenario(scenario);
  assert.equal(r.initial, 100000);
  assert.equal(r.stockEnd, 100000);
  assert.ok(Math.abs(r.benchmarkEnd - 161051) < 1e-6);
  assert.ok(Math.abs(r.annualNeeded - 12210.2) < 1e-6);
  assert.equal(r.benefitTotal, 75000);
  assert.equal(r.totalEnd, 175000);
  assert.equal(r.covers, true);
});
test('zero benefit stays a valid underperforming scenario', () => {
  const r = benefitScenario({...scenario, annualValue:0});
  assert.equal(r.covers, false);
  assert.equal(r.annualizedWithBenefits, 0);
});
test('benefits exactly covering the threshold tie the ETF', () => {
  const r = benefitScenario({...scenario, annualValue:12210.2});
  assert.equal(r.covers, true);
  assert.ok(Math.abs(r.annualizedWithBenefits - 10) < 1e-9);
});
test('no benefit required when the stock already wins', () => {
  assert.equal(benefitScenario({...scenario, stockCagr:12}).annualNeeded, 0);
});
test('invalid and missing inputs do not produce a verdict', () => {
  for (const change of [{annualValue:null},{annualValue:-1},{annualValue:Infinity},{price:0},{shares:0},{shares:1.5},{years:0},{stockCagr:-100},{benchmarkCagr:NaN}]) {
    assert.equal(benefitScenario({...scenario,...change}), null);
  }
});
test('period filter is independent of ten-year availability', () => {
  assert.equal(matches(period,'win'),true);
  assert.equal(matches({available:false},'win'),false);
  assert.equal(matches({available:false},'missing'),true);
  assert.equal(matches({...period,excess_cagr_pp:0},'win'),false);
  assert.equal(matches({...period,excess_cagr_pp:0},'lose'),false);
});
test('near filter uses annualized points, inclusive at one point', () => {
  assert.equal(matches({...period,excess_cagr_pp:-1},'near'),true);
  assert.equal(matches({...period,excess_cagr_pp:1.01},'near'),false);
});
test('rolling filter requires all 36 periods and at least 70 percent wins', () => {
  assert.equal(matches({...period,rolling:{complete:true,count:36,win_rate:72.2}},'rolling'),true);
  assert.equal(matches({...period,rolling:{complete:false,count:4,win_rate:100}},'rolling'),false);
  assert.equal(matches({...period,rolling:{complete:true,count:36,win_rate:69.4}},'rolling'),false);
});

const losingPeriods = () => Object.fromEntries([3,5,10].map(year => [String(year), {
  ...period, excess_cagr_pp:-6, rolling:{complete:true,count:36,max_gap:-5.5}
}]));
test('exclusion requires a gap of at least five annual points in all three horizons', () => {
  const ps = losingPeriods();
  assert.equal(underperformance(ps), 'exclude');
  ps['3'].excess_cagr_pp = -5;
  assert.equal(underperformance(ps), 'exclude');
  ps['3'].excess_cagr_pp = -4.999;
  assert.equal(underperformance(ps), 'keep');
  ps['3'].excess_cagr_pp = 2;
  assert.equal(underperformance(ps), 'keep');
});
test('missing, relisted or invalid ten-year data cannot cause exclusion', () => {
  const ps = losingPeriods();
  for (const p of [undefined, {available:false}, {...period,excess_cagr_pp:NaN}]) {
    ps['10'] = p;
    assert.equal(underperformance(ps), 'insufficient');
    assert.equal(underperformance(ps, 'rolling'), 'insufficient');
  }
  assert.equal(underperformance(undefined), 'insufficient');
});
test('shifted-date exclusion needs all 108 comparisons to be at least five points below', () => {
  const ps = losingPeriods();
  assert.equal(underperformance(ps, 'rolling'), 'exclude');
  ps['5'].rolling.max_gap = -5;
  assert.equal(underperformance(ps, 'rolling'), 'exclude');
  ps['5'].rolling.max_gap = -4.99;
  assert.equal(underperformance(ps, 'rolling'), 'keep');
  assert.equal(underperformance(ps), 'exclude');
  ps['5'].rolling.max_gap = 1;
  assert.equal(underperformance(ps, 'rolling'), 'keep');
});
test('partial rolling histories and malformed rolling data stay visible', () => {
  const ps = losingPeriods();
  for (const rolling of [undefined, {complete:false,count:20,max_gap:-5},
    {complete:true,count:35,max_gap:-5}, {complete:true,count:36,max_gap:NaN}]) {
    ps['10'].rolling = rolling;
    assert.equal(underperformance(ps, 'rolling'), 'insufficient');
    assert.equal(underperformance(ps), 'exclude');
  }
});
