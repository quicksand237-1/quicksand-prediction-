// Exercise the existing renderer with synthetic data; no server or HTTP calls.
const fs = require('node:fs');
const path = require('node:path');
const assert = require('node:assert/strict');
const html = fs.readFileSync(path.join(__dirname, 'forecast_dashboard.html'), 'utf8');
const script = html.match(/<script>([\s\S]*?)<\/script>/)[1];
new Function(script);
const ids = [...html.matchAll(/id="([^"]+)"/g)].map(match => match[1]);
assert.equal(new Set(ids).size, ids.length);
const renderer = script.match(/      function renderHistoricalMarketRows\(row\) \{[\s\S]*?\n      \}\n/)[0];
const body = {innerHTML: ''};
const render = new Function('$', 'number', 'fmt', 'pct', 'escapeHtml', `${renderer}; return renderHistoricalMarketRows;`)(
  () => body,
  value => value == null ? null : Number(value),
  (value, digits) => Number(value).toFixed(digits),
  value => value == null ? '-' : `${(Number(value) * 100).toFixed(1)}%`,
  value => String(value).replace(/[&<>"']/g, char => `&#${char.charCodeAt(0)};`),
);
render({outcome_market_rows: []});
assert.match(body.innerHTML, /当前 5 分钟区间没有/);
render({outcome_market_rows: [{latest_price: null, quality_flags: ['missing_price_snapshot', 'missing_orderbook_snapshot']}]});
assert.match(body.innerHTML, /当前 5 分钟区间无价格样本/);
assert.doesNotMatch(body.innerHTML, /未采集价格/);
const sample = {latest_price: 0, price_min: 0, price_max: 0, price_sample_count: 1,
  price_resolution_seconds: 10800, price_coarser_than_review_window: true,
  quality_status: 'audit_only', quality_flags: ['price_backfilled_after_checkpoint']};
render({outcome_market_rows: [sample]});
assert.match(body.innerHTML, /源采样 180 分钟（粗于当前区间）/);
assert.match(body.innerHTML, /0\.0%/);
assert.match(body.innerHTML, /事后补采历史价，仅供复盘/);
render({outcome_market_rows: [{...sample, price_resolution_seconds: null}]});
assert.doesNotMatch(body.innerHTML, /源采样/);
render({outcome_market_rows: [{...sample, price_source: 'archived_book_midpoint'}]});
assert.match(body.innerHTML, /盘口中间价代理/);
assert.doesNotMatch(body.innerHTML, /源采样/);
for (const [status,label] of Object.entries({attempted_missing:'已尝试但缺失',fetch_failed:'抓取失败',not_attempted:'尚未尝试',evidence_insufficient:'证据不足',read_error:'读取异常'})) {
  render({outcome_market_rows:[{latest_price:null,price_acquisition_status:status,
    price_acquisition_evidence:{finished_at_utc:'2026-10-03T00:00:00Z',start:'start',end:'end',
      errors:[{message:'<script>invalid</script>',http_status:429,trace_id:'trace'}]}}]});
  assert.ok(body.innerHTML.includes(label));
  assert.match(body.innerHTML,/2026-10-03T00:00:00Z/);
  assert.doesNotMatch(body.innerHTML,/<script>/);
}
console.log('Frontend syntax, unique DOM IDs, and synthetic price rendering passed; no service started.');
