const $ = id => document.getElementById(id);
const esc = value => String(value ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const pretty = value => esc(JSON.stringify(value, null, 2));
const labels = {ready:'Готов',running:'Работает',awaiting_input:'Ждёт уточнения',awaiting_approval:'Ждёт согласия',completed:'Заявка зарегистрирована',unverified:'Результат не подтверждён',failed:'Ошибка',budget_exceeded:'Лимит исчерпан',blocked:'Заблокирован',cancelled:'Остановлен',handoff:'Ждёт оператора',human_owned:'Управление у оператора'};
let config, session, timer, pendingRendered = '', actionBusy = false;

async function api(path, data) {
  const response = await fetch(path, data === undefined ? {} : {method:'POST', headers:{'Content-Type':'application/json','X-Lab-Request':'1'},body:JSON.stringify(data)});
  const body = await response.json();
  if (!response.ok) throw new Error(body.error || 'Ошибка запроса');
  return body;
}
function notice(text='') { $('notice').textContent = text; }
function chooseScenario() { const s = config.scenarios[$('scenario').value]; $('goal').value=s.goal; $('lesson').textContent=s.lesson; }
function schedule() { clearTimeout(timer); if(session?.busy) timer=setTimeout(poll,600); }
async function poll() { try { session=await api('/api/sessions/'+session.id); render(); schedule(); } catch(e){notice(e.message);} }

function render() {
  if (!session) return;
  const s=session, u=s.usage;
  $('status').textContent=labels[s.status]||s.status;
  $('start').disabled=s.busy || ['awaiting_input','awaiting_approval'].includes(s.status);
  $('cancel').disabled=!['running','ready','awaiting_input','awaiting_approval'].includes(s.status);
  $('export').disabled=false;
  $('metrics').innerHTML=`<span><b>${u.turns}</b> / ${s.limits.max_turns} обращений к LLM</span><span><b>${u.tool_calls}</b> / ${s.limits.max_tool_calls} исполнений</span><span><b>${u.active_seconds.toFixed(1)}</b> / ${s.limits.active_seconds} сек.</span><span><b>${u.tokens}</b> учтённых токенов</span><span>Стоимость: <b>${u.cost_usd===null?'неизвестна':'$'+u.cost_usd.toFixed(6)}</b>${u.cost_usd===null?'':' (оценка)'}</span>`;
  $('dialog').innerHTML=s.dialog.map(d=>`<div class="bubble ${esc(d.role)}"><small>${d.role==='user'?'ПОЛЬЗОВАТЕЛЬ':'МОДЕЛЬ · текст не является доказательством результата'}</small>${esc(d.text)}</div>`).join('');
  const openEvents=new Set([...$('trace').querySelectorAll('details[open]')].map(el=>el.dataset.event));
  $('trace').innerHTML=s.events.map(e=>`<div class="event ${esc(e.kind)}"><small>${e.n} · ${esc(e.kind)}</small><strong>${esc(e.title)}</strong>${Object.keys(e.data).length?`<details data-event="${e.n}" ${openEvents.has(String(e.n))?'open':''}><summary>Данные события</summary><pre>${pretty(e.data)}</pre></details>`:''}</div>`).join('');
  $('state').innerHTML=s.state.orders.map(o=>`<div class="record"><strong>Заказ ${esc(o.id)} · ${esc(o.item)}</strong>${o.amount.toLocaleString('ru-RU')} ₽ · версия ${o.version}${o.note?`<details><summary>Недоверенное примечание</summary><p>${esc(o.note)}</p></details>`:''}</div>`).join('')+`<h3>Заявки на возврат</h3>`+(s.state.refunds.length?s.state.refunds.map(r=>`<div class="record"><strong>${esc(r.id)} · ${r.amount.toLocaleString('ru-RU')} ₽</strong><span class="pill ${r.status==='REGISTERED'?'good':'warn'}">${esc(r.status)}</span><p>Деньги отправлены: <b>нет</b></p>${r.errors?`<p>${esc(r.errors.join('; '))}</p>`:''}</div>`).join(''):'<p class="empty">Ни одной записи. До подтверждения так и должно быть.</p>')+`<p class="muted">Вызовов записи: ${s.state.write_calls}. Успех здесь — регистрация заявки, не перевод денег.</p>`;
  renderPending();
  $('handoff').innerHTML=s.handoff?`<div class="transfer"><h3>Пакет передачи</h3><p>${esc(s.handoff.reason)}</p><p>Владелец: ${s.handoff.owner==='human_operator'?'оператор принял':'оператор ещё не принял'}</p><details><summary>Состояние и контекст</summary><pre>${pretty(s.handoff)}</pre></details>${s.status==='handoff'?'<button id="takeover">Я оператор: принять задачу</button>':''}</div>`:'';
  if($('takeover')) $('takeover').onclick=()=>action('takeover',{});
}
function renderPending(){
  const p=session.pending;
  if((p?.id||'')===pendingRendered) return;
  pendingRendered=p?.id||'';
  if(!p){$('human').innerHTML='';return;}
  if(p.kind==='question'){
    $('human').innerHTML=`<div class="question"><h3>Уточнение</h3><p>${esc(p.question)}</p><label>Ваш ответ<input id="answer" maxlength="2000" placeholder="Например: хочу вернуть деньги"></label><button id="reply">Ответить и продолжить</button><p class="muted">Ответ даёт информацию; он не разрешает запись.</p></div>`;
    $('reply').onclick=()=>action('resume',{pending_id:p.id,answer:$('answer').value});
  }else{
    $('human').innerHTML=`<div class="approval"><h3>Согласовать создание заявки?</h3><p>Заказ <b>${esc(p.args.order_id)}</b> · сумма <b>${p.args.amount.toLocaleString('ru-RU')} ₽</b> · на исходный способ оплаты.</p><p>Создастся учебная заявка. Деньги не переводятся.</p><div class="actions"><button id="approve">Подтвердить</button><button id="reject" class="danger">Отказать</button></div><details><summary>Эксперимент: подменить сумму в согласии</summary><label>Другая сумма<input id="changedAmount" type="number" min="1" value="${p.args.amount+100}"></label><button id="tamper" class="secondary">Отправить изменённые параметры</button><p class="muted">Harness должен отклонить согласие. Подготовленная операция останется прежней.</p></details><p class="muted">Согласие действительно ${session.limits.approval_seconds} сек. и привязано к параметрам и версии заказа.</p></div>`;
    $('approve').onclick=()=>action('resume',{pending_id:p.id,decision:'approve',args:p.args});
    $('reject').onclick=()=>action('resume',{pending_id:p.id,decision:'reject'});
    $('tamper').onclick=()=>action('resume',{pending_id:p.id,decision:'approve',args:{...p.args,amount:Number($('changedAmount').value)}});
  }
}
async function action(name,data){
  if(actionBusy)return;
  actionBusy=true;notice();
  try{session=await api('/api/sessions/'+session.id+'/'+name,data);render();schedule();}
  catch(e){notice(e.message);await poll();}
  finally{actionBusy=false;}
}
$('start').onclick=async()=>{
  if(actionBusy)return;
  actionBusy=true;$('start').disabled=true;notice();
  const limits={};
  for(const key of ['max_turns','max_tool_calls','max_retries','active_seconds','approval_seconds']) limits[key]=Number($(key).value);
  limits.max_usd=$('max_usd').value?Number($('max_usd').value):null;
  try{session=await api('/api/sessions',{scenario:$('scenario').value,goal:$('goal').value,limits});sessionStorage.setItem('lab-session',session.id);pendingRendered='';$('human').innerHTML='';render();schedule();}
  catch(e){notice(e.message);$('start').disabled=false;}
  finally{actionBusy=false;}
};
$('cancel').onclick=()=>action('cancel',{});
$('export').onclick=()=>{const url=URL.createObjectURL(new Blob([JSON.stringify(session,null,2)],{type:'application/json'}));const a=document.createElement('a');a.href=url;a.download='agent-trace-'+session.id.slice(0,8)+'.json';a.click();setTimeout(()=>URL.revokeObjectURL(url),1000);};
async function init(){try{
  config=await api('/api/config');
  $('scenario').innerHTML=Object.entries(config.scenarios).map(([id,s])=>`<option value="${esc(id)}">${esc(s.title)}</option>`).join('');
  $('scenario').onchange=chooseScenario;chooseScenario();
  $('connection').textContent=config.llm.configured?`Живая LLM: ${config.llm.model} · ${config.llm.base_url}`:'LLM не настроена · заполните .env и перезапустите сервер';
  $('tools').textContent=JSON.stringify(config.tools,null,2);
  if(!config.llm.configured) notice('Интерфейс готов. Для запуска нужны LLM_BASE_URL, LLM_API_KEY и LLM_MODEL в локальном .env.');
  const previous=sessionStorage.getItem('lab-session');
  if(previous){try{session=await api('/api/sessions/'+previous);$('scenario').value=session.scenario;$('goal').value=session.goal;$('lesson').textContent=config.scenarios[session.scenario].lesson;render();schedule();}catch{sessionStorage.removeItem('lab-session');}}
}catch(e){notice(e.message);}}
init();
