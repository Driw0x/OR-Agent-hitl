'use strict';
const $ = id => document.getElementById(id);
const stages = {
  received:'Demande reçue', extraction:'Extraction déterministe', data:'Vérification des données',
  constraints:'Analyse des contraintes', certificate:'Construction du certificat', routing:'Choix de la branche',
  generation:'Génération LLM', review:'Revue humaine', scope:'Confirmation de portée', approval:'Approbation',
  conversion:'Conversion du GAP', solver:'Résolution CP-SAT', verification:'Vérification indépendante', graphics:'Génération des graphiques'
};
const statuses = {pending:'En attente', running:'En cours', completed:'Terminée', blocked:'Bloquée', failed:'Échouée', not_applicable:'Non applicable'};
let sid = null, stream = null, current = null, dirty = false, savingProposal = null, pending = false, refreshing = false;
let revision = null;

function error(message) { $('error').textContent = message || ''; $('error').hidden = !message; }
async function api(path, body) {
  const options = body === undefined ? {} : {method:'POST', headers:{'Content-Type':'application/json','X-OR-Agent':'1'}, body:JSON.stringify(body)};
  const response = await fetch(path, options);
  const data = await response.json();
  if (!response.ok) throw Object.assign(new Error(data.error || `HTTP ${response.status}`), {status:response.status});
  return data;
}
function node(tag, content, className) {
  const element = document.createElement(tag);
  if (content !== undefined) element.textContent = String(content);
  if (className) element.className = className;
  return element;
}
function table(title, headers, rows) {
  const section = node('div'); section.append(node('h3',title));
  if (!rows.length) { section.append(node('p','Aucune donnée établie.','hint')); return section; }
  const wrap = node('div',undefined,'table-wrap'), t = node('table'), head = node('thead'), h = node('tr');
  for (const label of headers) h.append(node('th',label));
  head.append(h); t.append(head); const body = node('tbody');
  for (const row of rows) { const r = node('tr'); for (const value of row) r.append(node('td',typeof value === 'object' ? JSON.stringify(value) : value ?? 'Inconnu')); body.append(r); }
  t.append(body); wrap.append(t); section.append(wrap); return section;
}
function problemTable(problem) {
  const content = node('div');
  if (!problem) return content;
  content.append(table('GAP reconstruit',['Ressource',`Capacité (${problem.resource_unit})`],problem.resources.map((r,i)=>[r,problem.capacities[i]])));
  const rows = [];
  problem.resources.forEach((r,i)=>problem.tasks.forEach((t,j)=>rows.push([t,r,problem.costs[i][j],problem.consumption[i][j]])));
  content.append(table('Coûts et consommations',['Tâche','Ressource',`Coût (${problem.cost_unit})`,`Consommation (${problem.resource_unit})`],rows));
  return content;
}
function drawTimelineLines() {
  const root = $('timeline'), svg = root.querySelector('.timeline-lines');
  if (!svg) return;
  const bounds = root.getBoundingClientRect(), diagram = root.querySelector('.branch-diagram').getBoundingClientRect();
  const items = root.querySelectorAll('[data-stage]');
  const dot = item => {
    const box = item.getBoundingClientRect(), style = getComputedStyle(item, '::before');
    return {x:box.left-bounds.left+parseFloat(style.left)+parseFloat(style.width)/2,
      y:box.top-bounds.top+parseFloat(style.top)+parseFloat(style.height)/2};
  };
  const first = dot(items[0]), last = dot(items[items.length-1]);
  const top = diagram.top-bounds.top+24, bottom = top+48, middle = (top+bottom)/2;
  const split = first.x+18, merge = diagram.right-bounds.left-14, right = merge+12;
  const back = diagram.bottom-bounds.top-8, end = Math.max(last.y,diagram.bottom-bounds.top);
  svg.setAttribute('viewBox',`0 0 ${bounds.width} ${bounds.height}`);
  svg.querySelector('.branch-entry').setAttribute('d',`M${first.x} ${first.y} V${middle-6} Q${first.x} ${middle} ${first.x+6} ${middle} H${split}`);
  svg.querySelector('[data-branch=AUTONOMOUS]').setAttribute('d',`M${split} ${middle} V${top+6} Q${split} ${top} ${split+6} ${top} H${merge-6} Q${merge} ${top} ${merge} ${top+6} V${middle}`);
  svg.querySelector('[data-branch=HITL]').setAttribute('d',`M${split} ${middle} V${bottom-6} Q${split} ${bottom} ${split+6} ${bottom} H${merge-6} Q${merge} ${bottom} ${merge} ${bottom-6} V${middle}`);
  svg.querySelector('.branch-merge').setAttribute('d',`M${merge} ${middle} H${right-6} Q${right} ${middle} ${right} ${middle+6} V${back-6} Q${right} ${back} ${right-6} ${back} H${first.x+6} Q${first.x} ${back} ${first.x} ${back+6} V${end}`);
}
function timeline(data) {
  $('timeline').replaceChildren();
  function stageItem(stage) {
    const event = data?.stages?.[stage] || {status:'pending'};
    const li = node('li',undefined,'timeline-stage'); li.dataset.status = event.status; li.dataset.stage = stage;
    li.append(node('span',stages[stage],'stage-name'));
    let detail = statuses[event.status];
    if (event.duration_seconds !== undefined) detail += ` · ${event.duration_seconds.toFixed(3)} s`;
    if (event.status === 'running' && event.started_epoch) detail += ` · ${Math.max(0,Date.now()/1000-event.started_epoch).toFixed(1)} s écoulées`;
    li.append(node('span',detail,'stage-detail'));
    return li;
  }
  function group(label) {
    const li = node('li',undefined,'timeline-group');
    li.append(node('h3',label)); $('timeline').append(li); return li;
  }
  const initial = node('ol',undefined,'stage-list');
  for (const stage of ['received','extraction','data','constraints','certificate','routing']) initial.append(stageItem(stage));
  const start = group('Étapes communes initiales');
  start.classList.add('timeline-initial');
  start.append(initial);
  const branches = node('li',undefined,'timeline-group timeline-bifurcation');
  branches.setAttribute('aria-label','Bifurcation');
  $('timeline').append(branches);
  const branch = data?.stages?.routing?.details?.branch || data?.snapshot?.routing?.branch;
  const diagram = node('div',undefined,'branch-diagram');
  diagram.setAttribute('role','group');
  diagram.setAttribute('aria-label','Deux chemins alternatifs rejoignent les étapes finales communes.');
  for (const [key,label] of [
    ['AUTONOMOUS','Autonome'],
    ['HITL','Revue humaine / LLM facultatif']
  ]) {
    const selected = branch === key;
    const section = node('div',undefined,'timeline-branch');
    section.dataset.branch = key;
    section.dataset.selected = selected;
    section.dataset.inactive = !!branch && !selected;
    const head = node('div',undefined,'branch-head');
    head.append(node('h4',label),node('span',selected ? 'Empruntée' : branch ? 'Non empruntée' : 'En attente du choix','branch-state'));
    section.append(head);
    diagram.append(section);
  }
  branches.append(diagram);
  if (branch === 'HITL') {
    const list = node('ol',undefined,'stage-list branch-details');
    list.setAttribute('aria-label','Étapes de la revue humaine avec LLM facultatif');
    for (const stage of ['generation','review','scope','approval']) list.append(stageItem(stage));
    branches.append(list);
  }
  const final = node('ol',undefined,'stage-list');
  for (const stage of ['conversion','solver','verification','graphics']) {
    const executed = event => event.stage === stage && ['running','completed','failed'].includes(event.status);
    if (executed({...data?.stages?.[stage],stage}) || data?.events?.some(executed)) final.append(stageItem(stage));
  }
  if (final.children.length) {
    const end = group('Étapes communes finales');
    end.classList.add('timeline-final');
    end.append(final);
  }
  $('timeline').insertAdjacentHTML('beforeend','<svg class="timeline-lines" aria-hidden="true"><path class="branch-entry" vector-effect="non-scaling-stroke"/><path data-branch="AUTONOMOUS" vector-effect="non-scaling-stroke"/><path data-branch="HITL" vector-effect="non-scaling-stroke"/><path class="branch-merge" vector-effect="non-scaling-stroke"/></svg>');
  drawTimelineLines();
  $('eventLog').textContent = JSON.stringify(data?.events || [],null,2);
  $('activity').textContent = data?.active ? 'Traitement actif' : data ? 'Session enregistrée' : 'Prêt';
}
function render(data) {
  current = data; timeline(data); error(data.error);
  $('sessionPanel').hidden = false; $('sessionId').textContent = data.id;
  const state = data.snapshot;
  $('analyze').disabled = data.active || pending;
  if (!state) return;
  const hitl = state.hitl, cert = state.routing.certificate, auto = state.routing.branch === 'AUTONOMOUS';
  $('branch').textContent = (auto ? 'Autonome' : hitl.validation.source_blockers?.length ? 'Refus / clarification' : 'HITL')+(data.simulated_reviewer?' · TEST : confirmations simulées':'');
  $('mode').textContent = auto ? 'Mode : résolution déterministe sans LLM' : 'Mode : formulation assistée, confirmation humaine requise avant toute résolution.';
  const report = state.deterministic, established = report.established;
  $('s2').replaceChildren(
    node('p',`Certificat : ${cert.status}`,'hint'),
    table('Inventaires et unités',['Champ','Reconnu'],Object.entries(established).map(([k,v])=>[k,Array.isArray(v)?v.join(', '):v])),
    table('Faits extraits et associations',['Champ','Ressource','Tâche','Valeur','Unité','Vérification'],report.facts.map(f=>[f.field,f.resource,f.task,f.value,f.unit,f.verification])),
    table('Couverture numérique',['Champ','Vérifiés','Attendus'],Object.entries(report.coverage).map(([k,v])=>[k,v.verified,v.expected])),
    table('Contraintes reconnues',['Contrainte','Preuve source'],report.constraints.map(c=>[c.value,c.evidence])),
    table('Données manquantes',['Champ','Motif','Associations absentes'],report.unknown.filter(u=>u.reason).map(u=>[u.field,u.reason,u.missing])),
    table('Passages non interprétés',['Texte source'],report.uninterpreted.filter(u=>u.evidence.trim()).map(u=>[u.evidence])),
    table('Contradictions',['Diagnostic'],report.contradictions.map(c=>[c]))
  );
  $('technical').textContent = JSON.stringify({certificate:cert,notice:cert.scope},null,2);
  $('llmPanel').hidden = auto; $('reviewPanel').hidden = auto;
  if (!auto) {
    const token = hitl.review_token;
    if (revision !== token) { $('scopeCheck').checked=false; $('approveCheck').checked=false; revision=token; }
    $('original').textContent = state.original;
    if (savingProposal !== null && !data.active) {
      if (state.proposal === savingProposal) dirty = false;
      savingProposal = null;
    }
    if (!dirty) $('proposal').value = state.proposal;
    $('raw').textContent = state.raw || 'Aucune sortie disponible.';
    $('generationDetails').textContent = JSON.stringify(state.generation,null,2);
    const gen = data.stages.generation;
    $('generationStatus').textContent = state.generation ? `Appels : ${state.generation.calls} · Durée : ${state.generation.elapsed_seconds.toFixed(3)} s · ${state.generation.error || 'Sortie reçue ; validation humaine nécessaire.'}` : gen.status === 'running' ? (gen.details.connection || 'Génération en cours') : 'Ollama non sollicité.';
    $('diagnostics').replaceChildren(table('Validation de la proposition',['Catégorie','Diagnostic'],[
      ...hitl.validation.errors.map(e=>['Erreur',e]), ...hitl.validation.missing.map(e=>['Manquant',e]), ...hitl.validation.issues.map(e=>['Issue',e])
    ]));
    if (hitl.validation.complete) $('diagnostics').append(node('p','Structure complète. La fidélité à l’énoncé reste à confirmer.','hint'));
    $('canonical').replaceChildren(problemTable(hitl.validation.problem));
    if (!hitl.validation.problem && state.proposal_facts) {
      const proposed=state.proposal_facts;
      $('canonical').append(node('p','Données reconnues dans la proposition ; fidélité à l’énoncé non vérifiée.','hint'),
        table('Inventaires proposés',['Champ','Valeur'],Object.entries(proposed.established).map(([k,v])=>[k,v])),
        table('Faits proposés',['Champ','Ressource','Tâche','Valeur','Unité'],proposed.facts.map(f=>[f.field,f.resource,f.task,f.value,f.unit])));
    }
    $('approvalState').textContent = hitl.state;
    $('scopeState').textContent = hitl.scope_confirmed ? 'Portée confirmée pour cette version.' : 'Portée non confirmée.';
    $('fingerprint').textContent = `SHA-256 canonique : ${hitl.fingerprints.canonical_sha256 || 'indisponible'}`;
  } else {
    $('s2').append(problemTable(state.result?.problem || report.complete_problem));
  }
  buttons();
  const result = state.result;
  $('resultPanel').hidden = !result;
  if (result) {
    const isCurrent = state.result_current !== false && !dirty;
    $('historicalWarning').hidden = isCurrent;
    $('resultState').textContent = result.solver.raw_status || result.outcome;
    $('resultDetails').textContent = JSON.stringify(result,null,2);
    $('resultSummary').replaceChildren();
    const p = result.problem || result.approved_problem;
    const summary = [
      ['Objectif',`${result.solver.objective ?? '—'} ${p.cost_unit}`],
      ['Optimalité prouvée',result.optimality_proven?'Oui':'Non'],
      ['Vérification indépendante',result.verification?.valid?'Positive':result.verification?'Négative':'Sans candidat'],
      ['Temps CP-SAT',`${result.solver.solver_wall_seconds?.toFixed(4) ?? '—'} s`],
      ['Tâches / ressources',`${p.tasks.length} / ${p.resources.length}`],
      ['Approbation',result.approval_actor || 'Sans revue humaine']
    ];
    for (const [label,value] of summary) { const tile=node('div');tile.append(node('span',label),node('strong',value));$('resultSummary').append(tile); }
    $('exports').hidden = !isCurrent || data.active;
    $('resultFrame').hidden = !isCurrent || data.active;
    if (isCurrent && !data.active) {
      const base = `/api/sessions/${sid}`;
      $('svgExport').href=base+'/graph.svg'; $('htmlExport').href=base+'/result.html'; $('jsonExport').href=base+'/result.json';
      const frameURL=base+'/result.html?v='+(hitl?.fingerprints.proposal_sha256 || cert.certificate_sha256);
      if ($('resultFrame').getAttribute('src') !== frameURL) $('resultFrame').src=frameURL;
    }
  }
}
function buttons() {
  const state=current?.snapshot, h=state?.hitl, busy=!h || current.active || pending, locked=busy || h?.state==='REJECTED';
  const clean=!dirty, valid=h?.validation.complete, reviewed=h?.reviewed;
  $('dirtyNotice').hidden=!dirty;
  $('proposal').disabled=busy || h?.state==='REJECTED';
  $('save').disabled=locked || !dirty;
  $('validate').disabled=locked || !clean;
  $('review').disabled=locked || !clean;
  $('scope').disabled=locked || !clean || !valid || !reviewed || !$('scopeCheck').checked;
  $('solve').disabled=locked || !clean || !valid || !reviewed || !h?.scope_confirmed || !$('approveCheck').checked;
  $('reject').disabled=locked || !$('rejectReason').value.trim();
  $('generate').disabled=locked || !clean || !!state?.generation || !!h?.validation.source_blockers?.length || !!h?.approval_valid;
  if (dirty) { $('exports').hidden=true; $('resultFrame').hidden=true; $('historicalWarning').hidden=false; }
}
async function refresh() {
  if (!sid || refreshing===sid) return;
  const requested = sid; refreshing=requested;
  try { const data=await api(`/api/sessions/${sid}`);if(sid===requested) render(data); }
  catch(e) {
    if(sid!==requested) return;
    if(e.status===404) resetSession();
    error(e.message);$('exports').hidden=true;$('resultFrame').hidden=true;$('solve').disabled=true;
  }
  finally { if(refreshing===requested) refreshing=false; }
}
async function openSession(id) {
  if (stream) stream.close();
  sid=id; current=null;dirty=false;revision=null;savingProposal=null;
  localStorage.setItem('or-agent-session',sid);history.replaceState(null,'','#'+sid);
  $('history').hidden=true;
  await refresh();
  if(sid!==id || !current) return;
  const cursor=current.events.length;
  stream=new EventSource(`/api/sessions/${sid}/events?after=${cursor}`);
  stream.onopen=()=>{$('streamState').textContent='Flux SSE connecté';};
  stream.onerror=()=>{$('streamState').textContent='Reconnexion SSE… Aucun traitement relancé.';};
  stream.onmessage=()=>{refresh();};
  stream.addEventListener('deleted',()=>{if(sid===id){resetSession();error('Cette session a été supprimée.');}});
}
function resetSession() {
  if(stream) stream.close();
  sid=null;stream=null;current=null;dirty=false;revision=null;savingProposal=null;
  localStorage.removeItem('or-agent-session');history.replaceState(null,'',location.pathname+location.search);
  for(const id of ['sessionPanel','llmPanel','reviewPanel','resultPanel','exports','resultFrame']) $(id).hidden=true;
  for(const id of ['sessionId','streamState','s2','technical','original','raw','generationDetails','diagnostics','canonical','fingerprint','resultSummary','resultDetails']) $(id).replaceChildren();
  for(const id of ['source','proposal','rejectReason']) $(id).value='';
  for(const id of ['scopeCheck','approveCheck']) $(id).checked=false;
  for(const id of ['svgExport','htmlExport','jsonExport']) $(id).removeAttribute('href');
  $('resultFrame').removeAttribute('src');$('source').oninput();$('analyze').disabled=false;
  timeline(null);buttons();
}
async function action(name, body={}) {
  if (!sid || pending || current?.active) return;
  error(null);pending=true;buttons();
  try { await api(`/api/sessions/${sid}/${name}`,body); await refresh(); }
  catch(e) {error(e.message);savingProposal=null;}
  finally {pending=false;buttons();}
}
$('analyze').onclick=async()=>{
  const source=$('source').value;
  if(!source.trim() || source.length>8000) return error('Saisissez 1 à 8000 caractères.');
  try { pending=true;$('analyze').disabled=true;const data=await api('/api/sessions',{source});await openSession(data.id); }
  catch(e){error(e.message);}finally{pending=false;$('analyze').disabled=!!current?.active;}
};
$('source').oninput=()=>{$('counter').textContent=`${$('source').value.length} / 8 000`;};
for(const [id,key] of [['example','autonomous'],['proseExample','prose']]) $(id).onclick=async()=>{try{$('source').value=(await api('/api/examples'))[key];$('source').oninput();}catch(e){error(e.message);}};
$('reset').onclick=()=>{$('source').value='';$('source').oninput();};
$('proposal').oninput=()=>{dirty=$('proposal').value!==current?.snapshot?.proposal;$('scopeCheck').checked=false;$('approveCheck').checked=false;buttons();};
$('save').onclick=()=>{savingProposal=$('proposal').value;action('edit',{proposal:savingProposal,proposal_sha256:current.snapshot.hitl.fingerprints.proposal_sha256});};
for(const name of ['generate','validate','review']) $(name).onclick=()=>action(name);
$('scopeCheck').onchange=buttons;$('approveCheck').onchange=buttons;$('rejectReason').oninput=buttons;
$('scope').onclick=()=>{const token=current.snapshot.hitl.review_token;action('scope',{review_token:token,confirmation:'SCOPE:'+token});};
$('solve').onclick=()=>{
  if($('solve').disabled || !$('approveCheck').checked) return;
  const token=current.snapshot.hitl.review_token;
  action('resolve',{review_token:token,confirmation:'APPROVE:'+token});
};
$('reject').onclick=()=>action('reject',{reason:$('rejectReason').value});
async function showSessions() {
  try {
    const data=await api('/api/sessions');$('sessionList').replaceChildren();$('importList').replaceChildren();
    for(const s of data.sessions){
      const row=node('div',undefined,'session-card'), b=node('button',`${s.created_at} · ${s.id.slice(0,8)}${s.active?' · actif':''}`,'session-open');
      row.dataset.session=s.id;b.onclick=()=>openSession(s.id);
      const remove=node('button','×','danger session-delete');remove.disabled=s.active;
      remove.setAttribute('aria-label',`Supprimer la session ${s.id}`);
      remove.title=`Supprimer la session ${s.id}`;
      remove.onclick=async event=>{
        event.stopPropagation();
        if(remove.disabled || !window.confirm(`Supprimer définitivement la session ${s.id} et tous ses artefacts et résultats locaux ? Cette action est irréversible. La source d’un import reste conservée.`)) return;
        remove.disabled=true;
        try {
          await api(`/api/sessions/${s.id}/delete`,{confirmation:'DELETE:'+s.id});
          if(sid===s.id) resetSession();
          error(null);await showSessions();
        }catch(e){error(e.message);remove.disabled=s.active;}
      };
      row.append(b,remove);$('sessionList').append(row);
    }
    if(!data.sessions.length)$('sessionList').append(node('p','Aucune session enregistrée.'));
    for(const item of data.imports){const b=node('button',`Importer une copie : ${item.name}`);b.onclick=async()=>{try{await openSession((await api('/api/import',{id:item.id})).id);}catch(e){error(e.message);}};$('importList').append(b);}
    $('history').hidden=false;
  }catch(e){error(e.message);}
}
$('sessionsButton').onclick=showSessions;
$('closeHistory').onclick=()=>{$('history').hidden=true;};
timeline(null);
new ResizeObserver(drawTimelineLines).observe($('timeline'));
const saved=location.hash.slice(1)||localStorage.getItem('or-agent-session');
if(saved && /^[0-9a-f]{32}$/.test(saved))openSession(saved);
setInterval(()=>{if(current?.active)refresh();if(current)timeline(current);},750);
window.addEventListener('beforeunload',()=>{if(stream)stream.close();});
