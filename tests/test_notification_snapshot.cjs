const {test}=require('node:test');const assert=require('node:assert/strict');
const {buildSnapshot}=require('../notification_snapshot.cjs');
const {passesWatchlist}=require('../comparison.js');
const period=gap=>({available:true,stock_return:50,benchmark_return:100,excess_pp:-50,stock_cagr:5,benchmark_cagr:10,excess_cagr_pp:gap,end:'2026-09-11'});
function data(gap=-6,price=30){return {prices:{TEST:price},high52s:{TEST:100},low52s:{TEST:0},topix_comparisons:{TEST:{3:period(gap),5:period(gap),10:period(gap)}}};}
test('snapshot uses exact default range and TOPIX boundaries',()=>{
 const stocks=[{code:'TEST',name:'Test'}];let d=data();
 assert.equal(buildSnapshot(stocks,d,'2026-09-13T00:00:00Z').universe[0].eligible,false);
 d=data(-4.999,30);let s=buildSnapshot(stocks,d,'2026-09-13T00:00:00Z');assert.equal(s.universe[0].eligible,true);assert.equal(s.ready,true);
 d.prices.TEST=30.001;assert.equal(buildSnapshot(stocks,d,'2026-09-13T00:00:00Z').universe[0].eligible,false);
});
test('insufficient history is kept and coverage is recorded for regression detection',()=>{
 const d=data();d.topix_comparisons.TEST[10]={available:false};const s=buildSnapshot([{code:'TEST',name:'Test'}],d,'2026-09-13T00:00:00Z');assert.equal(s.universe[0].eligible,true);assert.deepEqual(s.universe[0].coverage,[3,5]);
 d.prices.TEST=null;assert.equal(buildSnapshot([{code:'TEST',name:'Test'}],d,'2026-09-13T00:00:00Z').ready,false);
});
test('manual screen filters can be cleared without changing notification defaults',()=>{
 const periods=data().topix_comparisons.TEST;
 assert.equal(passesWatchlist({rangePos:90,periods}),false);
 assert.equal(passesWatchlist({rangePos:90,periods},{rangeMax:NaN,hide:false}),true);
 assert.equal(passesWatchlist({rangePos:90,periods}),false);
});
