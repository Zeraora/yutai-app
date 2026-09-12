const {test} = require('node:test');
const assert = require('node:assert/strict');
const {matches, benefitScenario} = require('../comparison.js');

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
