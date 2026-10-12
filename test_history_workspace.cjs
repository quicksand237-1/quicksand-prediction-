// Synthetic browser checks using local Chrome CDP; never starts the production service.
const fs = require('node:fs'), http = require('node:http'), os = require('node:os');
const path = require('node:path'), {spawn} = require('node:child_process');
const assert = require('node:assert/strict');
const realUrl=process.argv[2];
const html = fs.readFileSync(path.join(__dirname, 'forecast_dashboard.html'), 'utf8');
new Function(html.match(/<script>([\s\S]*?)<\/script>/)[1]);
const ids = [...html.matchAll(/id="([^"]+)"/g)].map(x=>x[1]);
assert.equal(new Set(ids).size, ids.length);
let calls=0, slow=false, fail=false, invalidTime=false, reportDelay=0;
let csvSuccess=false, malformedPrivate=false, privateDelay=0;
let jobSequence=0,cancellations=0;const jobs=new Map();
let singleEmpty=false, holdFailure=false;
const publicCsv='slug,checkpoint_utc,current_count,model_forecast_final_count,data_quality_flags\r\ncsv-market,2026-08-20T00:05:00Z,1,10,missing_orderbook_snapshot\r\n';
const privateCsv='slug,checkpoint_utc,model_forecast_final_count,observed_local_final_count,resolved_bin,resolved_lower,resolved_upper,brier_score,log_loss,data_quality_flags,orderbook_snapshot_json\r\n'
  +'csv-market,2026-08-20T00:05:00Z,10,12,0-19,0,19,0.125,0.25,missing_orderbook_snapshot,"'
  +('ignored,""quoted""\r\n'.repeat(220000))+'"\r\n';
function fixture(slug) {
  return {ok:true,backtest_run_id:'synthetic',model_hash_sha256:'test-model',run_config:{paper_market_budget:100},summary:{markets:1,records:4,mean_brier_score:0.1234},
    coverage:{total_checkpoint_count:8,loaded_checkpoint_count:4,truncated:true},
    records:[0,1,2,3].map(i=>({slug,checkpoint_utc:`2026-08-20T00:${String((i+1)*5).padStart(2,'0')}:00Z`,
      current_count:i,elapsed_hours:(i+1)/12,model_forecast_final_count:10,forecast_bin:'0-19',forecast_bin_probability:0.7,
      bin_probabilities:{'0-19':0.7},outcome_market_rows:[],observed_local_final_count:10,
      resolved_bin:'0-19',resolved_lower:0,resolved_upper:19,brier_score:0.1234,
      paper_cash:99,paper_total_buy_cost:1,paper_total_sell_proceeds:0,paper_fees_paid:.02,paper_unrealized_pnl:0,
      ...(i===3?{paper_settlement_actions:[{outcome_label:'0-19',action:'settlement',reason:'settlement',quantity_delta:-2,position_after:0,realized_pnl_delta:1,realized_pnl:1,cash_flow:2,fee_paid:0}],paper_settlement_time_utc:'2026-08-21T00:00:00Z',paper_cash_after_settlement:101,paper_final_pnl:1,paper_final_return:1}:{}),
      position_actions:[{outcome_label:'0-19',point_in_time_safe:i>0,quantity_delta:i===3?2:0,
        position_after:i===3?2:0,realized_pnl:0,unrealized_pnl:i?0.01:null,
        action:i===2?'no_fill':i===3?'enter':'hold',partial_fill:i===3,reason:'test'}]}))};
}
const server=http.createServer((req,res)=>{
  const url=new URL(req.url,'http://localhost');
  if(url.pathname==='/'){res.setHeader('Content-Type','text/html; charset=utf-8');return res.end(html.replace(/\n    \}\)\(\);\s*<\/script>/,'\n      window.historyTest={loadRows,state,renderHistorySync};\n    })();\n  </script>'));}
  res.setHeader('Content-Type','application/json');
  if(csvSuccess && url.pathname.startsWith('/api/reports/')){
    const name=url.pathname.split('/').at(-1);
    res.setHeader('Content-Type','text/csv; charset=utf-8');
    const body=name==='public'?publicCsv:name==='summary'?'slug,status\ncsv-market,ok\n':malformedPrivate?'slug,checkpoint_utc\n"broken':privateCsv;
    let offset=0;
    const send=()=>{if(res.destroyed)return;if(offset>=body.length)return res.end();res.write(body.slice(offset,offset+16381));offset+=16381;setImmediate(send);};
    return setTimeout(send,name==='private'?privateDelay:0);
  }
  if(url.pathname==='/api/local-markets')return res.end(JSON.stringify({markets:['market-a','market-b'].map(slug=>({slug,window_end_utc:'2026-08-21'}))}));
  if(url.pathname==='/api/reports/public')return setTimeout(()=>{res.statusCode=500;res.end(JSON.stringify({message:'synthetic CSV failure'}));},reportDelay);
  if(url.pathname==='/api/backtest-review'){
    calls++;const slug=url.searchParams.get('slug');const body=fail?{ok:false,message:'synthetic failure'}:fixture(slug);
    if(singleEmpty && url.searchParams.get('paper_hold_summary')!=='1') {
      body.records=[];body.coverage={empty_message:'日期范围与市场不重叠'};
    }
    if(holdFailure && slug==='market-b' && url.searchParams.get('paper_hold_summary')==='1') {
      res.statusCode=500;return res.end(JSON.stringify({ok:false,message:'synthetic one market failure'}));
    }
    if(!fail && url.searchParams.get('paper_exit_policy')==='hold_to_settlement'){
      body.run_config={...body.run_config,paper_exit_policy:'hold_to_settlement',paper_mode:'mandatory_entry'};
      body.markets=[{slug,paper_entry_actual_utc:'2026-08-20T00:20:00Z',paper_settled:slug==='market-a',paper_total_buy_cost:10,paper_final_pnl:slug==='market-a'?2:null,paper_final_return:slug==='market-a'?.2:null}];
      if(url.searchParams.get('paper_hold_summary')==='1'){
        assert.equal(url.searchParams.has('start'),false);assert.equal(url.searchParams.has('end'),false);
        assert.equal(url.searchParams.get('paper_entry_elapsed_minutes'),'20');
      }
    }
    if(invalidTime&&!fail)body.records[0].checkpoint_utc='invalid-time';
    if(url.searchParams.get('async')==='1'){
      const id=String(++jobSequence);jobs.set(id,{body,ready:Date.now()+(slow&&slug==='market-a'?1000:0)});
      return res.end(JSON.stringify({ok:true,job_id:id,state:'queued',total:4,completed:0}));
    }
    return setTimeout(()=>res.end(JSON.stringify(body)),slow&&slug==='market-a'?250:0);
  }
  if(url.pathname.startsWith('/api/backtest-jobs/')){
    const id=url.pathname.split('/').at(-1),job=jobs.get(id);
    if(req.method==='DELETE'){cancellations++;job.cancelled=true;return res.end(JSON.stringify({ok:true,state:'cancelled'}));}
    return res.end(JSON.stringify({ok:true,job_id:id,state:job.cancelled?'cancelled':Date.now()<job.ready?'running':job.body.ok===false?'failed':'complete',message:job.body.message,result:job.body,total:4,completed:2}));
  }
  res.end(JSON.stringify({ok:true,state:'idle'}));
});
const pause=ms=>new Promise(resolve=>setTimeout(resolve,ms));
(async()=>{
  await new Promise(resolve=>server.listen(0,'127.0.0.1',resolve));
  const profile=fs.mkdtempSync(path.join(os.tmpdir(),'history-workspace-'));
  const browser=spawn('C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe',[
    '--headless=new','--disable-gpu','--no-first-run','--disable-background-networking',
    '--remote-debugging-port=0',`--user-data-dir=${profile}`,'about:blank'
  ],{windowsHide:true,stdio:['ignore','ignore','pipe']});
  let stderr='',ws;browser.stderr.on('data',b=>stderr+=b);
  const timeout=setTimeout(()=>{console.error('Browser timeout: '+stderr);browser.kill();server.close();process.exitCode=1;},realUrl?240000:30000);
  try {
    for(let i=0;i<100&&!stderr.includes('DevTools listening on');i++)await pause(100);
    const match=stderr.match(/DevTools listening on (ws:\/\/\S+)/);assert.ok(match,stderr);
    const debug=new URL(match[1]);const pages=await(await fetch(`http://${debug.host}/json/list`)).json();
    ws=new WebSocket(pages.find(page=>page.type==='page').webSocketDebuggerUrl);
    await new Promise(resolve=>ws.addEventListener('open',resolve,{once:true}));
    let sequence=0;const pending=new Map(),errors=[];
    ws.onmessage=e=>{const m=JSON.parse(e.data);if(m.id){const p=pending.get(m.id);pending.delete(m.id);m.error?p.reject(m.error):p.resolve(m.result);}if(m.method==='Runtime.exceptionThrown')errors.push(m.params.exceptionDetails);};
    const cdp=(method,params={})=>new Promise((resolve,reject)=>{const id=++sequence;pending.set(id,{resolve,reject});ws.send(JSON.stringify({id,method,params}));});
    const run=async expression=>{const r=await cdp('Runtime.evaluate',{expression,returnByValue:true,awaitPromise:true});if(r.exceptionDetails)throw Error(JSON.stringify(r.exceptionDetails));return r.result.value;};
    const wait=async expression=>{for(let i=0;i<100;i++){if(await run(expression))return;await pause(30);}throw Error('timeout '+expression+' '+JSON.stringify(errors)+' '+await run(`JSON.stringify({url:location.href, select:document.getElementById('marketSelect')?.outerHTML, status:document.readyState})`));};
    const click=id=>run(`document.getElementById('${id}').click()`);
    const content=id=>run(`document.getElementById('${id}').textContent`);
    const slide=i=>run(`(()=>{const e=document.getElementById('hourSlider');e.value=${i};e.dispatchEvent(new Event('input'));})()`);
    await cdp('Runtime.enable');await cdp('Page.enable');
    await cdp('Emulation.setDeviceMetricsOverride',{width:1440,height:1000,deviceScaleFactor:1,mobile:false});
    if(realUrl){
      assert.match(realUrl,/^http:\/\/127\.0\.0\.1:8778\/$/,'Only the isolated read-only debug server is allowed');
      await cdp('Page.navigate',{url:realUrl});
      for(let i=0;i<200;i++){if(await run(`document.getElementById('marketSelect')?.options.length>2`))break;await pause(100);}
      await click('paperTab');
      const a='elon-musk-of-tweets-september-11-september-18-2026',b='elon-musk-of-tweets-september-8-september-15-2026';
      const select=slug=>run(`(()=>{const e=document.getElementById('marketSelect');e.value='${slug}';e.dispatchEvent(new Event('change'));})()`);
      for(const slug of [a,b,a]){
        const started=Date.now();await select(slug);
        for(let i=0;i<700;i++){
          const status=await content('workspaceStatus');
          if(status.includes('历史读取失败'))throw Error(status);
          if(status.includes('已读取 2015/2015'))break;
          await pause(150);
        }
        assert.match(await content('workspaceStatus'),/已读取 2015\/2015/);
        assert.ok((await content('paperStatus')).includes(slug));
        await slide(11);const before=await content('paperActionBody');
        assert.match(before,/买入/);await slide(2);await slide(11);assert.equal(await content('paperActionBody'),before);
        console.log('Real readonly browser loaded '+slug+' '+((Date.now()-started)/1000).toFixed(1)+'s');
      }
      await select(a);await pause(700);await select(b);
      for(let i=0;i<700;i++){if((await content('workspaceStatus')).includes('已读取 2015/2015'))break;await pause(150);}
      assert.ok((await content('paperStatus')).includes(b));
      for(const width of [1440,390]){
        await cdp('Emulation.setDeviceMetricsOverride',{width,height:1000,deviceScaleFactor:1,mobile:width<500});
        await run(`document.getElementById('workspace').scrollIntoView()`);
        assert.equal(await run(`document.documentElement.scrollWidth<=innerWidth`),true);
        const shot=await cdp('Page.captureScreenshot',{format:'png'});
        fs.writeFileSync(path.join(os.tmpdir(),`history-real-${width}.png`),Buffer.from(shot.data,'base64'));
      }
      assert.deepEqual(errors,[]);console.log('Real readonly A-B-A, cancellation, paper fills and desktop/mobile passed.');
      return;
    }
    await cdp('Page.navigate',{url:`http://127.0.0.1:${server.address().port}/`});
    await wait(`document.getElementById('marketSelect')?.options.length===2`);
    assert.equal(await run(`document.getElementById('historyPriceFidelity').value`),'60');
    assert.deepEqual(await run(`[...document.getElementById('historyPriceFidelity').options].map(o=>o.value)`),['60','5']);
    await run(`window.historyTest.renderHistorySync({state:'sparse',include_prices:true,price_fidelity_minutes:60,succeeded:1,results:[{slug:'test-slug',ok:true,status:'sparse',price_coverage:[{market_id:'m',status:'sparse',covered_buckets:1,expected_buckets:24,request_count:2}],quality_warnings:[{message:'source sparse'}]}]})`);
    assert.match(await content('historySyncStatus'),/请求完成.*记录稀疏/);
    assert.match(await content('historyCoverageDetail'),/source sparse/);
    assert.doesNotMatch(await content('historySyncDetail'),/抓取异常/);
    await run(`window.historyTest.renderHistorySync({state:'running',market_progress:{completed_tokens:2,total_tokens:26,request_count:4,new_price_rows:12,elapsed_seconds:3},results:[]})`);
    assert.equal(await run(`document.getElementById('historyPriceFidelity').disabled`),true);
    assert.match(await content('historySyncDetail'),/盘口 2\/26/);
    await run(`window.historyTest.renderHistorySync({state:'idle',results:[]})`);
    assert.equal(await run(`document.getElementById('hourSlider').disabled`),true);
    assert.equal(await run(`document.querySelectorAll('#workspace input[type=range]').length`),1);
    await click('loadBacktestButton');await wait(`document.getElementById('hourSlider').max==='3'`);assert.equal(calls,1);
    await click('paperTab');assert.match(await content('paperPositionBody'),/买入（部分成交）/);
    assert.equal(await run(`document.getElementById('hourSlider').value`),'3');
    await click('paperUseCurrent');assert.equal(await run(`document.getElementById('paperEntryTime').value`),'0');assert.equal(await run(`document.getElementById('paperEntryMinutes').value`),'20');
    await click('paperFirstFill');assert.equal(await run(`document.getElementById('hourSlider').value`),'3');
    assert.match(await content('paperSettlement'),/待揭晓/);
    assert.doesNotMatch(await content('paperActionBody'),/到期兑付/);
    await slide(0);assert.match(await content('paperPositionBody'),/无安全历史盘口.*无持仓，无需估值/s);
    await slide(1);assert.match(await content('paperPositionBody'),/未触发交易/);
    await slide(2);assert.match(await content('paperPositionBody'),/尝试成交但未成交/);
    assert.doesNotMatch(await content('paperActionBody'),/尝试成交但未成交/);
    assert.match(await content('paperActionBody'),/没有已成立/);
    await slide(3);assert.match(await content('paperPositionBody'),/买入（部分成交）/);
    const before=await content('paperActionBody');await slide(0);await slide(3);assert.equal(await content('paperActionBody'),before);assert.equal(calls,1);
    await click('diagnosticTab');assert.match(await content('backtestMeanScores'),/揭晓/);
    await click('revealButton');assert.match(await content('backtestMeanScores'),/0.1234/);
    assert.match(await content('paperSettlement'),/最终现金 101.0000/);
    assert.match(await content('paperActionBody'),/官方结果到期兑付/);
    for(const id of ['historyChart','residualChart','globalResidualChart']) {
      await click(id==='historyChart'?'predictionTab':'diagnosticTab');
      await run(`(()=>{const c=document.getElementById('${id}');const r=c.getBoundingClientRect();c.dispatchEvent(new PointerEvent('pointermove',{clientX:r.left+r.width/2,clientY:r.top+100,pointerType:'mouse'}));})()`);
      const tip=await run(`document.getElementById('${id}').parentElement.querySelector('.chart-tooltip').textContent`);
      assert.match(tip,/北京时间/);assert.match(tip,/UTC/);assert.match(tip,/残差|预测 −/);
      await run(`document.getElementById('${id}').scrollIntoView()`);
      const chartShot=await cdp('Page.captureScreenshot',{format:'png'});
      fs.writeFileSync(path.join(os.tmpdir(),id+'-hover.png'),Buffer.from(chartShot.data,'base64'));
    }
    await click('revealButton');
    assert.equal(await run(`[...document.querySelectorAll('.chart-tooltip')].every(n=>n.classList.contains('hidden'))`),true);
    await click('revealButton');
    await click('paperTab');
    for(const width of [1440,390]){
      await cdp('Emulation.setDeviceMetricsOverride',{width,height:1000,deviceScaleFactor:1,mobile:width<500});
      await run(`document.getElementById('workspace').scrollIntoView()`);
      assert.equal(await run(`document.documentElement.scrollWidth<=innerWidth`),true,`overflow ${width}`);
      const shot=await cdp('Page.captureScreenshot',{format:'png'});
      fs.writeFileSync(path.join(os.tmpdir(),`history-workspace-${width}.png`),Buffer.from(shot.data,'base64'));
    }
    slow=true;await run(`document.getElementById('loadBacktestButton').click();const s=document.getElementById('marketSelect');s.value='market-b';s.dispatchEvent(new Event('change'))`);
    await wait(`window.historyTest.state.backtest?.records[0].slug==='market-b'`);await pause(350);
    assert.equal(await run(`window.historyTest.state.backtest.records[0].slug`),'market-b');assert.equal(await run(`window.historyTest.state.revealed`),false);
    // Completed A -> B -> A and cancellation after a job id has arrived.
    slow=false;
    for(const slug of ['market-a','market-b','market-a']){
      await run(`(()=>{const s=document.getElementById('marketSelect');s.value='${slug}';s.dispatchEvent(new Event('change'));})()`);
      await wait(`window.historyTest.state.backtest?.records[0].slug==='${slug}'`);
    }
    slow=true;await click('loadBacktestButton');await pause(100);
    await run(`(()=>{const s=document.getElementById('marketSelect');s.value='market-b';s.dispatchEvent(new Event('change'));})()`);
    await wait(`window.historyTest.state.backtest?.records[0].slug==='market-b'`);
    assert.ok(cancellations>0,'switch must cancel the old backend job');slow=false;
    fail=true;await click('loadBacktestButton');await wait(`document.getElementById('workspaceStatus').textContent.includes('synthetic failure')`);
    assert.equal(await content('paperActionBody'),'');assert.equal(await run(`document.getElementById('hourSlider').disabled`),true);
    fail=false;invalidTime=true;
    await click('loadBacktestButton');await wait(`document.getElementById('workspaceStatus').textContent.includes('响应市场或检查点不匹配')`);
    assert.equal(await run(`window.historyTest.state.backtest`),null);
    invalidTime=false;
    await click('loadBacktestButton');await wait(`window.historyTest.state.backtest?.records.length===4`);
    await click('loadFixedPublicButton');await wait(`document.getElementById('workspaceStatus').textContent.includes('synthetic CSV failure')`);
    assert.equal(await content('paperActionBody'),'');assert.equal(await run(`document.getElementById('hourSlider').disabled`),true);
    reportDelay=250;
    await click('loadFixedPublicButton');await click('loadBacktestButton');
    await wait(`window.historyTest.state.backtest?.records.length===4`);await pause(350);
    assert.match(await content('workspaceStatus'),/已读取 4/);
    assert.doesNotMatch(await content('localReportStatus'),/synthetic CSV failure/);
    await run(`window.historyTest.loadRows([{slug:'csv-market',checkpoint_utc:'2026-08-20T00:05:00Z',current_count:'1',model_forecast_final_count:'10'}],[])`);
    assert.match(await content('paperStatus'),/此数据源不含纸面交易记录/);
    csvSuccess=true;
    await click('loadFixedPublicButton');await wait(`document.getElementById('localReportStatus').textContent.includes('已加载固定公开预测')`);
    assert.match(await content('checkpointDiagnostics'),/missing_orderbook_snapshot/);
    await run(`window.csvTicks=0;window.csvTicker=setInterval(()=>window.csvTicks++,5)`);
    await click('loadFixedPrivateButton');await wait(`document.getElementById('localReportStatus').textContent.includes('私有结算已显式载入')`);
    assert.ok(await run(`window.csvTicks>0`),'streaming CSV should yield to UI');
    await run(`clearInterval(window.csvTicker)`);
    assert.equal(await run(`window.historyTest.state.privateRows.length`),1);
    assert.equal(await run(`window.historyTest.state.privateRows[0].orderbook_snapshot_json`),undefined);
    assert.equal(await run(`document.getElementById('answerContent').classList.contains('hidden')`),true);
    await click('revealButton');assert.equal(await content('actualCount'),'12');assert.equal(await content('checkpointBrier'),'0.125');
    malformedPrivate=true;await click('loadFixedPrivateButton');await wait(`document.getElementById('localReportStatus').textContent.includes('引号未闭合')`);
    assert.equal(await run(`document.getElementById('loadFixedPrivateButton').disabled`),false);
    assert.equal(await run(`window.historyTest.state.privateRows[0].observed_local_final_count`),'12');
    malformedPrivate=false;privateDelay=120;await click('loadFixedPrivateButton');
    await run(`document.getElementById('backtestStart').value='2026-08-20';document.getElementById('backtestStart').dispatchEvent(new Event('change'))`);
    await pause(200);assert.equal(await run(`window.historyTest.state.privateRows.length`),0);
    assert.equal(await content('paperPositionBody'),'');assert.deepEqual(errors,[]);
    await run(`document.getElementById('paperExitPolicy').value='hold_to_settlement';document.getElementById('paperEntryTime').value='0';document.getElementById('paperEntryMinutes').value='20'`);
    await click('loadBacktestButton');
    await wait(`document.getElementById('paperAllStatus').textContent.includes('统计完成 2/2')`);
    assert.match(await content('paperAllBody'),/未结算/);
    assert.match(await content('paperAllBody'),/揭晓后显示/);
    await click('revealButton');assert.match(await content('paperAllBody'),/20.0%/);
    assert.match(await content('paperAllBody'),/有效市场合计 1/);
    await run(`document.getElementById('backtestStart').dispatchEvent(new Event('change'))`);
    assert.equal(await content('paperAllBody'),'');
    singleEmpty=true;
    await click('loadBacktestButton');
    await wait(`document.getElementById('paperAllStatus').textContent.includes('统计完成 2/2')`);
    assert.match(await content('workspaceStatus'),/日期范围与市场不重叠/);
    assert.match(await content('paperAllBody'),/market-a/);assert.match(await content('paperAllBody'),/market-b/);
    holdFailure=true;
    await click('paperAllRun');
    await wait(`document.getElementById('paperAllStatus').textContent.includes('失败 1')`);
    assert.match(await content('paperAllBody'),/synthetic one market failure/);
    assert.match(await content('paperAllBody'),/有效市场合计 1/);
    holdFailure=false;
    await click('paperAllRun');
    await wait(`document.getElementById('paperAllStatus').textContent.includes('失败 0')`);
    assert.match(await content('paperAllBody'),/未结算/);
    singleEmpty=false;
    await click('fullMarketRangeButton');
    await wait(`window.historyTest.state.backtest?.records.length===4`);
    assert.equal(await run(`document.getElementById('backtestStart').value`),'');
    assert.equal(await run(`document.getElementById('backtestEnd').value`),'');
    console.log('Synthetic browser checks passed. Screenshots: '+os.tmpdir());
  } finally {clearTimeout(timeout);if(ws)ws.close();browser.kill();server.close();}
})().catch(error=>{console.error(error);process.exitCode=1;server.close();});
