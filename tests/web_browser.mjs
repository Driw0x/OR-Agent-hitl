// Real Chrome/Chromium over CDP. Node >=22; no browser library or DOM mock.
import assert from 'node:assert/strict';
import {readFileSync,writeFileSync} from 'node:fs';
import {setTimeout as delay} from 'node:timers/promises';

const [base, debuggerURL, fixturesPath] = process.argv.slice(2);
const fixtures = JSON.parse(readFileSync(fixturesPath, 'utf8'));
const target = await (await fetch(`${debuggerURL}/json/new?about:blank`, {method:'PUT'})).json();
const socket = new WebSocket(target.webSocketDebuggerUrl);
await new Promise((resolve, reject) => {socket.onopen=resolve; socket.onerror=reject;});
let sequence = 0;
const pending = new Map(), errors = [], dialogs = [];
socket.onmessage = event => {
  const data = JSON.parse(event.data);
  if (data.id) {
    const request = pending.get(data.id);
    if (!request) return;
    pending.delete(data.id); clearTimeout(request.timer);
    if (data.error) request.reject(new Error(JSON.stringify(data.error)));
    else request.resolve(data.result);
  }
  if (data.method === 'Runtime.exceptionThrown') errors.push(data.params.exceptionDetails);
  if (data.method === 'Runtime.consoleAPICalled' && data.params.type === 'error') errors.push(data.params.args);
  if (data.method === 'Page.javascriptDialogOpening') dialogs.push(data.params);
};
function cdp(method, params={}) {
  return new Promise((resolve, reject) => {
    const id=++sequence;
    const timer=setTimeout(() => {pending.delete(id); reject(new Error(`CDP timeout: ${method}`));}, 15000);
    pending.set(id,{resolve,reject,timer}); socket.send(JSON.stringify({id,method,params}));
  });
}
async function evaluate(expression) {
  const result=await cdp('Runtime.evaluate',{expression,awaitPromise:true,returnByValue:true});
  assert(!result.exceptionDetails, JSON.stringify(result.exceptionDetails));
  return result.result.value;
}
async function wait(expression) {
  for (let attempt=0; attempt<200; attempt++) {
    if (await evaluate(expression)) return;
    await delay(50);
  }
  throw new Error(`Browser condition timed out: ${expression}\n${await evaluate('document.body.innerText')}`);
}
async function check(expression, message) {assert(await evaluate(expression), message || expression);}
async function click(id) {
  await wait(`document.getElementById(${JSON.stringify(id)}) && !document.getElementById(${JSON.stringify(id)}).disabled`);
  const point=await evaluate(`(() => {
    const element=document.getElementById(${JSON.stringify(id)});
    element.scrollIntoView({block:'center'});
    const r=element.getBoundingClientRect();
    if (!r.width || !r.height || r.left<0 || r.right>innerWidth) throw new Error('Action inaccessible: '+element.id+' '+JSON.stringify({left:r.left,right:r.right,width:r.width,viewport:innerWidth}));
    return {x:r.x+r.width/2,y:r.y+r.height/2};
  })()`);
  await cdp('Input.dispatchMouseEvent',{type:'mousePressed',button:'left',clickCount:1,...point});
  await cdp('Input.dispatchMouseEvent',{type:'mouseReleased',button:'left',clickCount:1,...point});
}
async function fill(id, value) {
  await evaluate(`(() => {const e=document.getElementById(${JSON.stringify(id)});e.value=${JSON.stringify(value)};e.dispatchEvent(new Event('input',{bubbles:true}));})()`);
}
async function state() {return evaluate(`fetch('/api/sessions/'+document.getElementById('sessionId').textContent).then(r=>r.json())`);}
async function idle() {
  await wait(`typeof current !== 'undefined' && current?.snapshot && !current.active && !pending && !refreshing && !document.getElementById('analyze').disabled`);
}
async function start(source) {
  const before=await evaluate(`document.getElementById('sessionId').textContent`);
  await fill('source',source); await click('analyze');
  await wait(`document.getElementById('sessionId').textContent !== ${JSON.stringify(before)}`);
  await idle();
  const data=await state(); assert.equal(data.error,null); return data.id;
}
async function action(id, condition) {
  const events=(await state()).events.length;
  await click(id);
  await wait(`current.events.length > ${events} && !current.active`);
  await idle();
  assert.equal((await state()).error,null);
  if (condition) await check(condition);
}
async function history() {
  if (await evaluate(`!document.getElementById('history').hidden`)) await click('closeHistory');
  await click('sessionsButton');
  await wait(`!document.getElementById('history').hidden`);
}
async function layout(width) {
  await cdp('Emulation.setDeviceMetricsOverride',{width,height:1000,deviceScaleFactor:1,mobile:width===390});
  await delay(100);
  await check(`document.documentElement.scrollWidth <= innerWidth+1`, `Global horizontal overflow at ${width}px`);
  await check(`document.querySelector('#resultFrame').contentDocument.documentElement.scrollWidth <= document.querySelector('#resultFrame').clientWidth+1`, `Result overflow at ${width}px`);
}
async function exportsAndViews(result) {
  await wait(`!document.getElementById('resultFrame').hidden && document.getElementById('resultFrame').contentDocument?.querySelector('svg .assignment')`);
  const exported=await evaluate(`(async()=>{
    const out={};
    for(const id of ['jsonExport','svgExport','htmlExport']) {
      const response=await fetch(document.getElementById(id).href);
      out[id]={status:response.status,type:response.headers.get('content-type'),text:await response.text()};
    }
    return out;
  })()`);
  for (const item of Object.values(exported)) assert.equal(item.status,200);
  assert.match(exported.jsonExport.type,/application\/json/);
  assert.match(exported.svgExport.type,/image\/svg\+xml/);
  assert.match(exported.htmlExport.type,/text\/html/);
  assert.deepEqual(JSON.parse(exported.jsonExport.text),result);
  const rendered=await evaluate(`(() => {
    const d=document.getElementById('resultFrame').contentDocument;
    return {title:d.querySelector('h1').textContent,
      rows:[...d.querySelectorAll('tbody tr')].map(r=>[...r.children].map(c=>c.textContent)),
      loads:[...d.querySelectorAll('section')].map(s=>({resource:s.querySelector('h3').textContent,load:+s.querySelector('meter').value,capacity:+s.querySelector('meter').max})),
      links:[...d.querySelectorAll('svg .assignment title')].map(e=>e.textContent),
      svg:d.querySelector('svg').outerHTML};
  })()`);
  const p=result.problem || result.approved_problem;
  assert.match(rendered.title,new RegExp(`OPTIMAL · ${result.verification.recomputed_objective}`));
  assert.deepEqual(rendered.rows,result.solver.candidate.assignments.map(a=>{
    const i=p.resources.indexOf(a.resource),j=p.tasks.indexOf(a.task);
    return [a.task,a.resource,String(p.costs[i][j]),String(p.consumption[i][j])];
  }));
  assert.deepEqual(rendered.loads,p.resources.map((resource,i)=>({resource,load:result.verification.loads[resource],capacity:p.capacities[i]})));
  for (const a of result.solver.candidate.assignments) {
    const i=p.resources.indexOf(a.resource),j=p.tasks.indexOf(a.task);
    const title=`${a.task} : ${a.resource}; coût ${p.costs[i][j]} ${p.cost_unit}; consommation ${p.consumption[i][j]} ${p.resource_unit}`;
    assert(rendered.links.includes(title)); assert(exported.svgExport.text.includes(title));
    assert(exported.htmlExport.text.includes(title));
  }
  await check(`document.getElementById('resultDetails').textContent === JSON.stringify(current.snapshot.result,null,2)`);
  await check(`document.getElementById('resultSummary').textContent.includes('Positive')`);
  for (const width of [1440,1280,390]) await layout(width);
}
async function reloadWithoutWork() {
  const before=await state();
  await cdp('Page.reload'); await wait(`document.getElementById('sessionId')?.textContent === ${JSON.stringify(before.id)}`); await idle();
  const after=await state();
  assert.deepEqual(after.events,before.events); assert.deepEqual(after.snapshot.result,before.snapshot.result);
  return before;
}

try {
  await cdp('Page.enable'); await cdp('Runtime.enable');
  await cdp('Emulation.setDeviceMetricsOverride',{width:1440,height:1000,deviceScaleFactor:1,mobile:false});
  await cdp('Page.navigate',{url:base}); await wait(`document.getElementById('analyze') && typeof current !== 'undefined'`);
  const autoId=await start(fixtures.source);
  const auto=await state(), result=auto.snapshot.result;
  assert.equal(auto.snapshot.routing.branch,'AUTONOMOUS');
  assert.equal(auto.snapshot.routing.certificate.status,'VERIFIED_COMPLETE');
  assert.equal(result.llm_calls,0); assert.equal(auto.snapshot.generation,null);
  assert.equal(auto.stages.generation.status,'not_applicable');
  assert.equal(result.solver.raw_status,'OPTIMAL'); assert.equal(result.optimality_proven,true);
  assert.equal(result.verification.valid,true); assert.equal(result.solver.objective,6);
  assert.deepEqual(result.problem,fixtures.problem);
  assert.deepEqual(result.verification.loads,{R1:4,R2:2});
  assert.deepEqual(result.solver.candidate.assignments,[{task:'T1',resource:'R1'},{task:'T2',resource:'R2'},{task:'T3',resource:'R1'}]);
  await check(`document.getElementById('reviewPanel').hidden && document.getElementById('llmPanel').hidden`);
  await exportsAndViews(result); await reloadWithoutWork();
  console.log('PASS A: README deterministic GAP, optimum 6, independent check, assignments, capacities, JSON/SVG/HTML; 1440/1280/390px.');

  // Manual review stays usable after an explicitly controlled unavailable backend.
  const humanId=await start(fixtures.prose);
  assert.equal((await state()).snapshot.routing.branch,'HITL');
  await check(`!document.getElementById('diagnostics').hidden && document.getElementById('solve').disabled`);
  await action('generate');
  const unavailable=await state();
  assert.equal(unavailable.snapshot.generation.calls,0);
  assert.match(unavailable.snapshot.generation.error,/admission refused/);
  assert.equal(unavailable.stages.generation.status,'failed');
  await check(`document.getElementById('generationStatus').textContent.includes('admission refused')`);
  await fill('proposal',fixtures.proposal);
  await action('save'); await action('validate');
  // Browser HTTP request deliberately bypasses the disabled UI; server must refuse.
  await evaluate(`api('/api/sessions/'+current.id+'/solve',{})`); await wait('!current.active && !!current.error');
  assert.match((await state()).error,/approval/i);
  assert.equal((await state()).snapshot.result,null);
  await action('review'); await click('scopeCheck'); await action('scope');
  const approvedProblem=(await state()).snapshot.hitl.validation.problem;
  await check(`document.getElementById('solve').disabled`);
  await click('approveCheck'); await action('solve');
  const solved=await state();
  assert.equal(solved.snapshot.hitl.approval_valid,true);
  assert.deepEqual(solved.snapshot.result.approved_problem,approvedProblem);
  assert.equal(solved.snapshot.result.verification.valid,true);
  assert.equal(solved.snapshot.result.solver.objective,12);
  await exportsAndViews(solved.snapshot.result); await reloadWithoutWork();
  const oldToken=solved.snapshot.hitl.review_token;
  await fill('proposal',fixtures.proposal.replace('cost=7 euro','cost=8 euro'));
  await check(`document.getElementById('exports').hidden && document.getElementById('resultFrame').hidden && document.getElementById('solve').disabled`);
  await action('save');
  const changed=await state();
  assert.equal(changed.snapshot.hitl.approval_valid,false); assert.equal(changed.snapshot.hitl.scope_confirmed,false);
  assert.equal(changed.snapshot.result_current,false); assert.notEqual(changed.snapshot.hitl.review_token,oldToken);
  await check(`!document.getElementById('historicalWarning').hidden && !document.getElementById('scopeCheck').checked && !document.getElementById('approveCheck').checked`);
  const refused=await evaluate(`Promise.all(['result.json','graph.svg','result.html'].map(async name=>(await fetch('/api/sessions/'+current.id+'/'+name)).status))`);
  assert(refused.every(status=>status===409));
  await evaluate(`api('/api/sessions/'+current.id+'/resolve',{review_token:${JSON.stringify(oldToken)},confirmation:${JSON.stringify('APPROVE:'+oldToken)}})`);
  await wait('!current.active && !!current.error');
  assert.equal((await state()).snapshot.result_current,false);
  await action('review'); await click('scopeCheck'); await action('scope'); await click('approveCheck'); await action('solve');
  assert.equal((await state()).snapshot.result.solver.objective,13);
  await exportsAndViews((await state()).snapshot.result);
  console.log('PASS B: manual HITL, server refusal without approval, exact approved GAP, edit invalidation, stale token/exports refused, new approval required; 1440/1280/390px.');

  const generatedId=await start(fixtures.prose); await action('generate');
  const generated=await state();
  assert.equal(generated.snapshot.generation.calls,1); assert.equal(generated.snapshot.raw,fixtures.proposal);
  assert.equal(generated.snapshot.proposal,fixtures.proposal); assert.equal(generated.snapshot.hitl.approval_valid,false);
  assert.equal(generated.snapshot.result,null); await reloadWithoutWork();
  console.log('PASS D: controlled unavailable Ollama and one controlled draft response in real browser; no real LLM generation.');

  await history();
  await wait(`document.querySelector('[data-session="${autoId}"] .session-open')`);
  await evaluate(`document.querySelector('[data-session="${autoId}"] .session-open').id='reopen'`); await click('reopen');
  await wait(`current?.id === ${JSON.stringify(autoId)} && !current.active`); await idle();
  assert.deepEqual((await state()).events,auto.events);
  assert.deepEqual((await state()).snapshot.result,result); await exportsAndViews(result);
  // Imports and deletion exercise the existing browser test's original guarantees.
  await history(); await wait(`document.querySelector('#importList button')`);
  await evaluate(`document.querySelector('#importList button').id='import'`); await click('import');
  await wait(`current?.imported === '0' && current.snapshot && !current.active`); await idle();
  const importedId=(await state()).id;
  assert.equal((await state()).snapshot.hitl.approval_valid,false);
  for (const id of [autoId,humanId,generatedId,importedId]) {
    await history(); await wait(`document.querySelector('[data-session="${id}"] .session-delete')`);
    await evaluate(`document.querySelector('[data-session="${id}"] .session-delete').id='deleteSession'`);
    const deleting=click('deleteSession');
    for(let i=0;!dialogs.length && i<100;i++) await delay(20);
    assert(dialogs.shift()?.message.includes(id)); await cdp('Page.handleJavaScriptDialog',{accept:true});
    await deleting;
    await wait(`!document.querySelector('[data-session="${id}"]')`);
  }
  await check(`!localStorage.getItem('or-agent-session') && !location.hash`);
  assert.deepEqual(errors,[],'Blocking JavaScript or console errors');
  console.log('PASS C: reload, saved session consultation without rerun, diagnostics/SSE, import, deletion, mobile actions, no JavaScript errors or global overflow.');
} catch (error) {
  try {
    const screenshot=await cdp('Page.captureScreenshot',{format:'png'});
    writeFileSync(fixturesPath+'.png',Buffer.from(screenshot.data,'base64'));
  } catch (captureError) {console.error('Screenshot unavailable:',captureError.message);}
  throw error;
} finally {
  socket.close();
}
