"use strict";

const state={user:null,csrf:"",system:null,view:"dashboard",taskId:null,publicIntake:location.pathname==="/help",intake:{need:"food",postal_code:"02038",results:[],referrals:{}}};
const $=(selector,root=document)=>root.querySelector(selector);
const $$=(selector,root=document)=>[...root.querySelectorAll(selector)];
const content=$("#content");

function esc(value){return String(value??"").replace(/[&<>'"]/g,char=>({"&":"&amp;","<":"&lt;",">":"&gt;","'":"&#39;",'"':"&quot;"}[char]));}
function words(value){return String(value||"").replaceAll("_"," ").replace(/\b\w/g,c=>c.toUpperCase());}
function relative(value){if(!value)return "now";const seconds=Math.max(0,(Date.now()-new Date(value).getTime())/1000);if(seconds<60)return "just now";if(seconds<3600)return `${Math.floor(seconds/60)}m ago`;if(seconds<86400)return `${Math.floor(seconds/3600)}h ago`;return `${Math.floor(seconds/86400)}d ago`;}
function callControl(task,system){const waiting=task.scheduled_for&&new Date(task.scheduled_for)>new Date();if(waiting)return `<button class="mini-button" disabled title="${esc(new Date(task.scheduled_for).toLocaleString())}">Retry scheduled</button>`;if(system.telephony_provider==="twilio")return `<button class="mini-button" data-call-task="${esc(task.id)}" ${task.consent_active?"":"disabled"}>${task.consent_active?"Place call":"Consent required"}</button>`;return `<button class="mini-button" data-run-task="${esc(task.id)}">Call the provider</button>`;}
function toast(message,error=false){const node=document.createElement("div");node.className=`toast${error?" error":""}`;node.textContent=message;$("#toast-region").append(node);setTimeout(()=>node.remove(),3600);}
function loading(){content.innerHTML='<div class="empty"><div class="empty-icon">V</div><p class="muted">Loading the latest evidence…</p></div>';}

async function api(path,options={}){
  const headers={Accept:"application/json",...(options.headers||{})};
  if(options.body&&typeof options.body!=="string"){headers["Content-Type"]="application/json";options.body=JSON.stringify(options.body);}
  if(state.csrf&&options.method&&options.method!=="GET")headers["X-CSRF-Token"]=state.csrf;
  const response=await fetch(path,{credentials:"same-origin",...options,headers});
  if(response.status===204)return null;
  const data=await response.json().catch(()=>({error:{message:"Invalid server response"}}));
  if(!response.ok){const error=new Error(data.error?.message||`Request failed (${response.status})`);error.status=response.status;throw error;}
  return data;
}

let formDialogResolver=null;
function formDialog({kicker="Curator action",title,copy="",submit="Continue",fields=""}){
  $("#form-dialog-kicker").textContent=kicker;$("#form-dialog-title").textContent=title;$("#form-dialog-copy").textContent=copy;$("#form-dialog-submit").textContent=submit;$("#form-dialog-fields").innerHTML=fields;$("#form-dialog-error").textContent="";
  $("#form-dialog").showModal();return new Promise(resolve=>{formDialogResolver=resolve;});
}
function closeFormDialog(value=null){if(formDialogResolver){const resolve=formDialogResolver;formDialogResolver=null;resolve(value);}$("#form-dialog").close();}
$("#form-dialog-form").addEventListener("submit",event=>{event.preventDefault();closeFormDialog(Object.fromEntries(new FormData(event.currentTarget)));});
$("#form-dialog-cancel").addEventListener("click",()=>closeFormDialog());
$("#form-dialog-close").addEventListener("click",()=>closeFormDialog());
$("#form-dialog").addEventListener("cancel",event=>{event.preventDefault();closeFormDialog();});

async function boot(){
  if(state.publicIntake){showPublicIntake();renderIntake();return;}
  try{const session=await api("/api/auth/me");setSession(session);showApp();await navigate("dashboard");}
  catch(error){if(error.status!==401)toast(error.message,true);showLogin();}
}
function setSession(data){state.user=data.user;state.csrf=data.csrf_token;const initials=data.user.display_name.split(/\s+/).map(x=>x[0]).join("").slice(0,2).toUpperCase();$("#user-avatar").textContent=initials;}
function showLogin(){$("#login-view").hidden=false;$("#app-view").hidden=true;$("#app-view").classList.remove("public-mode");}
function showApp(){$("#login-view").hidden=true;$("#app-view").hidden=false;$("#app-view").classList.remove("public-mode");}
function showPublicIntake(){$("#login-view").hidden=true;$("#app-view").hidden=false;$("#app-view").classList.add("public-mode");}

$("#login-form").addEventListener("submit",async event=>{
  event.preventDefault();const button=event.currentTarget.querySelector("button");button.disabled=true;$("#login-error").textContent="";
  try{const form=new FormData(event.currentTarget);const data=await api("/api/auth/login",{method:"POST",body:{username:form.get("username"),password:form.get("password")}});setSession(data);showApp();await navigate("dashboard");}
  catch(error){$("#login-error").textContent=error.message;}finally{button.disabled=false;}
});
$("#logout-button").addEventListener("click",async()=>{try{await api("/api/auth/logout",{method:"POST"});}finally{state.user=null;state.csrf="";showLogin();}});
$("#menu-button").addEventListener("click",()=>$(".sidebar").classList.toggle("open"));
$$('[data-view]').forEach(button=>button.addEventListener("click",()=>navigate(button.dataset.view)));

const titles={dashboard:["Today","Overview"],decisions:["Human judgment","Decision inbox"],directory:["HSDS 3.2","Resource directory"],intake:["Loop B","Find verified help"],activity:["Append-only","Evidence trail"]};
async function navigate(view){
  state.view=view;$$('[data-view]').forEach(button=>button.classList.toggle("active",button.dataset.view===view));$(".sidebar").classList.remove("open");
  $("#page-kicker").textContent=titles[view][0];$("#page-title").textContent=titles[view][1];loading();
  try{if(view==="dashboard")await renderDashboard();if(view==="decisions")await renderDecisions();if(view==="directory")await renderDirectory();if(view==="intake")renderIntake();if(view==="activity")await renderActivity();}
  catch(error){content.innerHTML=`<div class="empty"><div class="empty-icon">!</div><h2>We couldn’t load this view</h2><p class="muted">${esc(error.message)}</p><button class="button secondary" data-action="retry">Try again</button></div>`;}
}

async function renderDashboard(){
  const [data,system]=await Promise.all([api("/api/dashboard"),api("/api/system")]);
  state.system=system;
  $("#decision-badge").hidden=!data.decisions;$("#decision-badge").textContent=data.decisions;$("#agent-mode").textContent=`${system.agent_mode} mode`;
  $("#audit-health").textContent=system.audit_chain_valid?"● Evidence chain intact":"● Evidence chain warning";
  const queue=data.queue.filter(item=>item.status==="queued");
  content.innerHTML=`
    <div class="hero"><div><p class="eyebrow">The report becomes the call</p><h2>${data.fresh} door${data.fresh===1?"":"s"} confirmed open. <em>${data.decisions} call${data.decisions===1?"":"s"} you should make.</em></h2><p class="muted">On the Line stays on the phone with providers so nobody finds out the hard way. It stops where a person should decide.</p></div><button class="button secondary" data-action="refresh-queue">Refresh queue ↻</button></div>
    <div class="stats">
      <article class="stat"><span class="label">Doors confirmed open</span><strong>${data.fresh}<span class="stat-of">/${data.services}</span></strong><small>Heard from the provider in the last 30 days</small></article>
      <article class="stat"><span class="label">Calls you did not make</span><strong>${data.autonomous}</strong><small>Tier 0 and 1, every one traceable to a quote</small></article>
      <article class="stat attention"><span class="label">Needs your judgment</span><strong>${data.decisions}</strong><small>Closures, ambiguity, or eligibility</small></article>
      <article class="stat"><span class="label">Reports that became calls</span><strong>${data.failures}</strong><small>Someone hit a closed door; it went to the front</small></article>
    </div>
    <div class="grid-2">
      <section class="panel"><div class="panel-head"><h3>Next to verify</h3><button class="text-button" data-view-jump="directory">Open directory</button></div>
        <div>${queue.length?queue.map((task,index)=>`<div class="queue-item${task.trigger==="seeker_failure"?" from-neighbour":""}"><span class="rank">${index+1}</span><div><strong>${esc(task.service_name)}</strong><small>${esc(task.organization_name)} · ${task.trigger==="seeker_failure"?'<b class="neighbour-tag">Reported closed by a neighbour</b>':esc(words(task.trigger))} · ${task.fields.map(esc).join(", ")}</small></div><div class="priority"><span class="priority-score">${Math.round(task.priority)} priority</span>${callControl(task,system)}</div></div>`).join(""):'<div class="empty"><p class="muted">The queue is clear.</p></div>'}</div>
      </section>
      <section class="panel"><div class="panel-head"><h3>Open doors</h3><span class="eyebrow">Last 30 days</span></div><div id="freshness-ring" class="freshness-ring"><strong>${data.freshness}%</strong></div><p class="ring-caption">of listed services confirmed open by the people who run them</p><div class="legend"><div><span>Confirmed open</span><strong>${data.fresh}</strong></div><div><span>Waiting to be called</span><strong>${data.queued}</strong></div><div><span>Reported closed by a neighbour</span><strong>${data.failures}</strong></div></div></section>
    </div>`;
  $("#freshness-ring").style.setProperty("--fresh",data.freshness);
}

async function renderDecisions(){
  const data=await api("/api/decisions");$("#decision-badge").hidden=!data.decisions.length;$("#decision-badge").textContent=data.decisions.length;
  if(!data.decisions.length){content.innerHTML='<div class="panel empty"><div class="empty-icon">✓</div><h2>No decisions waiting</h2><p class="muted">On the Line is making the routine calls and will stop here when one is yours to make.</p><button class="button secondary" data-view-jump="dashboard">Return to overview</button></div>';return;}
  content.innerHTML=`<div class="hero"><div><p class="eyebrow">${data.decisions.length} open</p><h2>The calls that are <em>yours to make.</em></h2><p class="muted">The model cannot publish any of these changes.</p></div></div><div class="decision-list">${data.decisions.map(item=>`
    <article class="decision-card"><div class="decision-top"><div><p class="eyebrow">${esc(item.organization_name)}</p><h3>${esc(item.service_name)}</h3><p class="muted">${esc(item.reason)}</p></div><span class="tier tier-${item.tier}">Tier ${item.tier} · review</span></div>
      <div class="diff"><div class="diff-block"><small>Current ${esc(words(item.field_name))}</small><strong>${esc(item.old_value||"Not recorded")}</strong></div><div class="diff-arrow">→</div><div class="diff-block"><small>Proposed</small><strong>${esc(item.new_value||"Remove value")}</strong></div></div>
      <blockquote class="quote">“${esc(item.quote)}”</blockquote>
      <div class="decision-actions">${item.kind!=="change"?`<button class="button primary" data-decision="acknowledge" data-id="${esc(item.id)}">Acknowledge review</button>`:`<button class="button danger" data-decision="reject" data-id="${esc(item.id)}">Reject</button><button class="button ghost" data-decision="edit" data-id="${esc(item.id)}" data-value="${esc(item.new_value||"")}">Edit & apply</button><button class="button primary" data-decision="approve" data-id="${esc(item.id)}">Approve change</button>`}</div>
    </article>`).join("")}</div>`;
}

async function renderDirectory(query=""){
  const data=await api(`/api/services?q=${encodeURIComponent(query)}`);
  content.innerHTML=`<div class="hero"><div><p class="eyebrow">Open Referral HSDS 3.2</p><h2>A directory with <em>receipts.</em></h2><p class="muted">Every assurance date and field change traces back to evidence.</p></div><button class="button secondary" data-action="import-hsds">Import HSDS JSON</button></div><div class="toolbar"><input id="directory-search" class="search" type="search" value="${esc(query)}" placeholder="Search services, organizations, or needs"><button class="button secondary" data-action="search-directory">Search</button></div><div class="table-wrap"><table class="data-table"><thead><tr><th>Service</th><th>Contact</th><th>Schedule</th><th>Location</th><th>Assurance</th><th>Provider</th></tr></thead><tbody>${data.services.map(item=>`<tr><td><strong>${esc(item.name)}</strong><span class="muted">${esc(item.organization_name)} · ${esc(item.taxonomy)}</span></td><td>${esc(item.phone||"—")}</td><td>${esc(item.byday||"—")}<br><span class="muted">${esc(item.opens_at||"")}–${esc(item.closes_at||"")}</span></td><td>${esc(item.address_1||"")}<br><span class="muted">${esc(item.city||"")}, ${esc(item.region||"")} ${esc(item.postal_code||"")}</span></td><td><span class="status-dot"></span><span class="age ${(item.age_days??999)>30?"stale":""}">${item.age_days??"Never"}${item.age_days!=null?"d":""}</span><br><span class="muted">${esc(item.assured_date||"Unverified")}</span></td><td><div class="table-actions"><button class="mini-button" data-consent-org="${esc(item.organization_id)}" data-consent-phone="${esc(item.organization_phone||item.phone||"")}" data-consent-timezone="${esc(item.organization_timezone||"America/New_York")}">Record consent</button></div></td></tr>`).join("")}</tbody></table></div>`;
}

function renderIntake(){
  const publicHeader=state.publicIntake?`<header class="public-header"><a class="wordmark" href="/"><span class="brand-mark" aria-hidden="true"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round"><path d="M8 12h8"/><circle cx="4.6" cy="12" r="3.2" fill="currentColor" stroke="none"/><circle cx="19.4" cy="12" r="3.2" fill="currentColor" stroke="none"/></svg></span> On the Line</a><a class="curator-link" href="/">Curator sign in →</a></header>`:"";
  content.innerHTML=`${publicHeader}<div class="hero"><div><p class="eyebrow">Before you travel</p><h2>Help that someone <em>picked up the phone</em> about.</h2><p class="muted">Every listing here shows when a person at that organization last confirmed it. If you find a closed door, tell us and it becomes the next call we make.</p></div></div><div class="intake-shell"><section class="panel intake-form"><form id="intake-form"><label>What kind of help is needed?<input name="need" value="${esc(state.intake.need)}" placeholder="Food, housing, legal help…" required minlength="2"></label><label>Postal code<input name="postal_code" value="${esc(state.intake.postal_code)}" placeholder="Optional"></label><button class="button primary wide" type="submit">Find verified services <span>→</span></button></form><p class="demo-note">No name or contact details are needed to search.</p></section><section class="panel"><div class="panel-head"><h3>Matching services</h3><span class="eyebrow">Freshness first</span></div><div id="intake-results" class="result-list">${renderIntakeResults()}</div></section></div>`;
  $("#intake-form").addEventListener("submit",searchIntake);
}
function renderIntakeResults(){if(!state.intake.results.length)return '<div class="empty"><div class="empty-icon">↗</div><p class="muted">Tell us what is needed and we will show what was confirmed open.</p></div>';return state.intake.results.map(item=>`<article class="result-card"><h3>${esc(item.name)}</h3><p class="muted">${esc(item.description)}</p><div class="result-meta"><span class="chip">${esc(item.taxonomy)}</span><span class="chip fresh">${esc(item.freshness_label)}</span><span class="chip">${esc(item.postal_code)}</span></div><strong>${esc(item.phone||"Call information unavailable")}</strong><div class="result-actions"><small>${esc(item.byday||"")} ${esc(item.opens_at||"")}–${esc(item.closes_at||"")}</small><div><button class="mini-button" data-report-service="${esc(item.id)}">Report a problem</button> <button class="mini-button" data-referral-service="${esc(item.id)}">Use referral</button></div></div></article>`).join("");}
async function searchIntake(event){event.preventDefault();const form=new FormData(event.currentTarget);state.intake.need=String(form.get("need"));state.intake.postal_code=String(form.get("postal_code"));try{const data=await api("/api/intake/search",{method:"POST",body:{need:state.intake.need,postal_code:state.intake.postal_code,limit:8}});state.intake.results=data.results;$("#intake-results").innerHTML=renderIntakeResults();}catch(error){toast(error.message,true);}}

async function renderActivity(){
  const data=await api("/api/audit");$("#audit-health").textContent=data.chain_valid?"● Evidence chain intact":"● Evidence chain warning";
  content.innerHTML=`<div class="hero"><div><p class="eyebrow">SHA-256 hash chain</p><h2>Every action leaves <em>evidence.</em></h2><p class="muted">Events cannot be edited or deleted; each hash depends on the event before it.</p></div><span class="health-pill">${data.chain_valid?"● Chain verified":"● Chain failed"}</span></div><section class="panel timeline">${data.events.map(event=>`<div class="activity-item"><span class="event-icon">${event.event_type.includes("failure")?"!":"✓"}</span><div><strong>${esc(words(event.event_type.replace("."," ")))}</strong><small>${esc(event.actor)} · ${esc(event.entity_type)} ${esc(event.entity_id)}</small></div><div><small>${relative(event.created_at)}</small><div class="hash">${esc(event.event_hash.slice(0,12))}…</div></div></div>`).join("")}</section>`;
}

content.addEventListener("click",async event=>{
  const jump=event.target.closest("[data-view-jump]");if(jump){navigate(jump.dataset.viewJump);return;}
  const retry=event.target.closest('[data-action="retry"]');if(retry){navigate(state.view);return;}
  const refresh=event.target.closest('[data-action="refresh-queue"]');if(refresh){refresh.disabled=true;try{const data=await api("/api/campaign/refresh",{method:"POST"});toast(`Queue refreshed: ${data.updated} reprioritized, ${data.created} created`);await renderDashboard();}catch(error){toast(error.message,true);refresh.disabled=false;}return;}
  const run=event.target.closest("[data-run-task]");if(run){state.taskId=run.dataset.runTask;resetCallDialog(run.closest(".queue-item")?.querySelector("small")?.textContent.split(" · ")[0]||"the provider");$("#demo-dialog").showModal();return;}
  const call=event.target.closest("[data-call-task]");if(call){call.disabled=true;try{const result=await api(`/api/tasks/${encodeURIComponent(call.dataset.callTask)}/call`,{method:"POST"});toast(`Call ${result.call_id.slice(-6)} is dialing`);await renderDashboard();}catch(error){toast(error.message,true);call.disabled=false;}return;}
  const importer=event.target.closest('[data-action="import-hsds"]');if(importer){$("#hsds-import-file").click();return;}
  const consent=event.target.closest("[data-consent-org]");if(consent){await recordConsent(consent);return;}
  const search=event.target.closest('[data-action="search-directory"]');if(search){renderDirectory($("#directory-search").value);return;}
  const decision=event.target.closest("[data-decision]");if(decision){await decide(decision);return;}
  const referral=event.target.closest("[data-referral-service]");if(referral){try{const result=await api("/api/intake/referrals",{method:"POST",body:{service_id:referral.dataset.referralService,need:state.intake.need,postal_code:state.intake.postal_code}});state.intake.referrals[referral.dataset.referralService]=result.referral_id;toast("Referral saved. Call ahead if you can, and tell us if the door is shut.");}catch(error){toast(error.message,true);}return;}
  const report=event.target.closest("[data-report-service]");if(report){await reportProblem(report.dataset.reportService);}
});

async function decide(button){
  let action=button.dataset.decision;let edited_value=null;
  const isEdit=action==="edit";const isReject=action==="reject";const isAcknowledge=action==="acknowledge";
  const values=await formDialog({kicker:isAcknowledge?"Review complete":"Human decision",title:isEdit?"Edit and apply this change":isReject?"Reject this proposal":isAcknowledge?"Acknowledge this review":"Approve this change",copy:isAcknowledge?"Your note is retained with the safety evidence.":"This decision is written to the append-only evidence trail.",submit:isReject?"Reject proposal":isAcknowledge?"Acknowledge":"Confirm decision",fields:`${isEdit?`<label>Correct value<input name="edited_value" value="${esc(button.dataset.value||"")}" required maxlength="2000"></label>`:""}<label>Decision note<textarea name="note" maxlength="1000" ${isReject?"required":""} placeholder="Record the evidence behind this decision"></textarea></label>`});
  if(!values)return;if(isEdit)edited_value=values.edited_value;const note=values.note||"";
  button.disabled=true;try{await api(`/api/decisions/${encodeURIComponent(button.dataset.id)}`,{method:"POST",body:{action,edited_value,note}});toast(isAcknowledge?"Review acknowledged":action==="reject"?"Proposal rejected":"Directory updated with human approval");await renderDecisions();}catch(error){toast(error.message,true);button.disabled=false;}
}
async function reportProblem(serviceId){
  const values=await formDialog({kicker:"Tell us what you found",title:"This listing was wrong",copy:"We call the provider to check. Your contact details are redacted before the report is stored.",submit:"Send it to the front of the queue",fields:'<label>What went wrong?<select name="reason" required><option value="wrong_hours">The hours were wrong</option><option value="disconnected">The number was disconnected</option><option value="closed">The service was closed</option><option value="ineligible">Eligibility was wrong</option><option value="moved">The service moved</option><option value="other">Another problem</option></select></label><label>Details<textarea name="details" maxlength="1000" placeholder="What happened?"></textarea></label>'});
  if(!values)return;
  try{const data=await api("/api/intake/failures",{method:"POST",body:{service_id:serviceId,reason:values.reason,details:values.details||"",referral_id:state.intake.referrals[serviceId]||null}});const reported=state.intake.results.find(item=>item.id===serviceId);toast(`Thank you. ${reported?reported.organization_name||reported.name:"That service"} is now the next call we make.`);}catch(error){toast(error.message,true);}
}

async function recordConsent(button){
  const today=new Date();const until=new Date(today);until.setFullYear(until.getFullYear()+1);const iso=value=>value.toISOString().slice(0,10);
  const values=await formDialog({kicker:"Provider consent",title:"Authorize verification calls",copy:"The scheduler enforces this exact number, time window, validity period, and call cap.",submit:"Record consent",fields:`<label>Consented business number<input name="phone_number" value="${esc(String(button.dataset.consentPhone||"").replace(/[^+\d]/g,""))}" required></label><label>IANA timezone<input name="timezone" value="${esc(button.dataset.consentTimezone)}" required></label><label>Earliest local time<input name="earliest_local" type="time" value="09:00" required></label><label>Latest local time<input name="latest_local" type="time" value="17:00" required></label><label>Valid from<input name="valid_from" type="date" value="${iso(today)}" required></label><label>Valid until<input name="valid_until" type="date" value="${iso(until)}" required></label><label>Maximum calls<input name="max_calls" type="number" value="5" min="1" max="10000" required></label><label>Consented by<input name="consented_by" maxlength="300" required></label><label>Evidence reference<input name="evidence_reference" maxlength="1000" placeholder="Signed form or agreement reference" required></label>`});
  if(!values)return;
  try{await api("/api/consents",{method:"POST",body:{organization_id:button.dataset.consentOrg,phone_number:values.phone_number,timezone:values.timezone,allowed_days:["MO","TU","WE","TH","FR"],earliest_local:values.earliest_local,latest_local:values.latest_local,valid_from:values.valid_from,valid_until:values.valid_until,max_calls:Number(values.max_calls),recording_allowed:false,consented_by:values.consented_by,evidence_reference:values.evidence_reference}});toast("Provider consent is active");await renderDirectory();}catch(error){toast(error.message,true);}
}

$("#hsds-import-file").addEventListener("change",async event=>{const file=event.target.files[0];if(!file)return;try{const document=JSON.parse(await file.text());const services=document.services||document.contents;if(!Array.isArray(services))throw new Error("The JSON must contain a services or contents array");const result=await api("/api/directory/import",{method:"POST",body:{services}});toast(`Imported ${result.services} services from ${result.organizations} organizations`);await renderDirectory();}catch(error){toast(error.message,true);}finally{event.target.value="";}});

$("#scenario-select").addEventListener("change",event=>{const copy={confirmed:"Tier 0 · assurance only",hours_changed:"Tier 1 · safe field update",program_closed:"Tier 2 · human decision",uncertain_volunteer:"Tier 2 · ambiguity",stop_calling:"Tier 3 · stop and suppress"};$("#scenario-policy").textContent=copy[event.target.value];});
const CALL_GAP=0.35;let callManifest=null;
async function loadCallScript(scenario){if(!callManifest)callManifest=await(await fetch("/assets/call/manifest.json")).json();return callManifest[scenario]||[];}
function resetCallDialog(who){$("#call-setup").hidden=false;$("#call-stage").hidden=true;$("#call-verdict").hidden=true;$("#call-transcript").innerHTML="";$("#call-clock").textContent="0:00";$("#call-status").textContent="Connecting";$("#call-who").textContent=who;const button=$("#run-demo-button");button.dataset.mode="call";button.disabled=false;button.hidden=false;button.textContent="Place the call →";}
function clock(seconds){return `${Math.floor(seconds/60)}:${String(Math.floor(seconds%60)).padStart(2,"0")}`;}
function callLine(turn){const readback=turn.role==="assistant"&&/read that back/i.test(turn.text);const node=document.createElement("div");node.className=`call-line ${turn.role}${readback?" readback":""}`;node.innerHTML=`<span>${turn.role==="assistant"?"On the Line":"Provider"}${readback?" · readback":""}</span><p>${esc(turn.text)}</p>`;const list=$("#call-transcript");list.append(node);list.scrollTop=list.scrollHeight;document.dispatchEvent(new CustomEvent("otl:call-turn",{detail:{role:turn.role,text:turn.text}}));}
async function playCall(scenario){
  const turns=await loadCallScript(scenario);
  $("#call-setup").hidden=true;$("#call-stage").hidden=false;$("#call-status").textContent="On the line";
  let elapsed=0;const tick=setInterval(()=>{elapsed+=1;$("#call-clock").textContent=clock(elapsed);},1000);
  try{
    const fast=Boolean(window.__OTL_CALL_FAST);
    for(const turn of turns){
      callLine(turn);
      if(!fast&&!window.__OTL_MUTE_CALL){const audio=new Audio(`/assets/call/${turn.file}`);audio.play().catch(()=>{});}
      await new Promise(resolve=>setTimeout(resolve,(fast?0.85:turn.seconds+CALL_GAP)*1000));
    }
  }finally{clearInterval(tick);}
  $("#call-status").textContent="Call ended";$(".call-dot").style.animation="none";
}
$("#scenario-select").addEventListener("change",event=>{const copy={confirmed:"Tier 0 · assurance only",hours_changed:"Tier 1 · safe field update",program_closed:"Tier 2 · human decision",uncertain_volunteer:"Tier 2 · ambiguity",stop_calling:"Tier 3 · stop and suppress"};$("#scenario-policy").textContent=copy[event.target.value];});
$("#run-demo-button").addEventListener("click",async event=>{
  const button=event.currentTarget;
  if(button.dataset.mode==="done"){const review=state.callResult&&state.callResult.status==="needs_review";const tier=state.callResult?state.callResult.tier:0;$("#demo-dialog").close();toast(review?`Tier ${tier}: sent to your decision inbox`:`Tier ${tier}: applied with the provider's own words`);await navigate(review?"decisions":"dashboard");return;}
  button.disabled=true;button.textContent="On the line…";
  const scenario=$("#scenario-select").value;
  try{
    await playCall(scenario);
    button.textContent="Reading the call…";
    const result=await api(`/api/tasks/${encodeURIComponent(state.taskId)}/demo`,{method:"POST",body:{scenario}});
    const review=result.status==="needs_review";
    const verdict=$("#call-verdict");verdict.className=`call-verdict${review?" review":""}`;
    const said=result.extraction&&result.extraction.summary;verdict.innerHTML=`<strong>Tier ${result.tier} · ${review?"held for a person":"applied to the directory"}</strong><small>${esc(said||(review?"A person decides this one.":"Published with the provider's own words attached."))}</small>`;
    verdict.hidden=false;
    state.callResult=result;
    button.disabled=false;button.dataset.mode="done";button.textContent=review?"Open the decision inbox →":"Back to the overview →";
    document.dispatchEvent(new CustomEvent("otl:call-verdict",{detail:result}));
  }catch(error){toast(error.message,true);button.disabled=false;button.hidden=false;button.textContent="Place the call →";}
});

boot();
