'use strict';
const $ = id => document.getElementById(id);
const busyStates = new Set(['connecting', 'scanning', 'running', 'paused', 'stopping']);
const statusLabels = {disconnected:'等待连接',connecting:'正在连接',ready:'设备已就绪',scanning:'识别中',running:'正在执行',paused:'已暂停',stopping:'正在停止',stopped:'已停止',finished:'本轮已结束',needs_attention:'需要手动处理',error:'操作失败'};
const phaseLabels = {locating:'LOCATING CAMPAIGN',ready:'READY TO GO',opening:'OPENING TASK',browsing:'BROWSING IN PROGRESS',returning:'BACK TO YOUR TASKS',scanning:'FINDING MORE TASKS',finished:'SESSION FINISHED',stopped:'SESSION STOPPED',needs_attention:'YOUR ATTENTION NEEDED'};
const taskLabels = {pending:'待执行',running:'执行中',done:'已核验',manual:'手动处理',failed:'未通过',disabled:'已跳过'};
const kindLabels = {browse:'浏览',search:'搜索',visit:'跳转',claim:'领奖',manual:'手动',done:'完成'};
const deviceUIPreferences=new Map();
let instanceHash='', queueOrder=[], clonesEnabled=false, settingsSection=null;
let state, token='', filter='all', taskHash='', logsHash='', initialized=false, requesting=false, toastTimer;
let pollRequest=null, pollTimer, previewTimer, previewRequest=null, previewReady=false, pageActive=true, lastPreviewSequence=null, polling=false, previewing=false;
const clockText=seconds=>{seconds=Math.max(0,Math.floor(seconds||0));return `${String(Math.floor(seconds/60)).padStart(2,'0')}:${String(seconds%60).padStart(2,'0')}`;};
function el(tag, cls, text) {const n=document.createElement(tag);if(cls)n.className=cls;if(text!==undefined)n.textContent=text;return n;}
function toast(text){$('toast').textContent=text;$('toast').hidden=false;clearTimeout(toastTimer);toastTimer=setTimeout(()=>$('toast').hidden=true,6000);}
function config(){const swipes={};for(const name of ['distance','duration','interval','burst'])for(const bound of ['min','max'])swipes['swipe_'+name+'_'+bound]=Number($('swipe-'+name+'-'+bound).value)/(name==='distance'?100:1);return {...swipes,default_browse_seconds:Number($('browse-seconds').value),browse_extra_seconds:Number($('browse-extra').value),max_attempts:Number($('max-attempts').value),max_tasks:Number($('max-tasks').value),enabled_task_kinds:['browse','search','visit','claim'].filter(k=>$('kind-'+k).checked),allow_cross_app:$('cross-app').checked,ocr:$('ocr').checked,scroll_browse:$('scroll').checked,claim_rewards:$('claim').checked};}
function defaultInstanceId(){const instances=state?.app_instances||[];return (instances.find(i=>i.user_id===0&&i.package===state?.catalog?.find(app=>app.id===state.app)?.package)||instances.find(i=>i.user_id===0)||instances[0])?.id||'';}
function instanceProblem(){
  if(!state?.connected||!state.catalog?.some(app=>app.activities?.length))return '';
  if(!(state.app_instances||[]).length)return '未检测到当前应用的可执行实例。请刷新实例，并检查应用安装情况、USB 调试与安全设置权限。';
  if(!state.selected_instance||!state.app_instances.some(item=>item.id===state.selected_instance))return '原选择的应用实例已失效。请打开应用分身，手动选择要执行的实例；不会自动切换到其他账号。';
  return '';
}
function showSettings(name){
  settingsSection=name;
  if(name&&!$('common-settings').open)$('common-settings').showModal();
  for(const button of document.querySelectorAll('[data-settings-toggle]')){const active=button.dataset.settingsToggle===name;button.classList.toggle('is-open',active);button.setAttribute('aria-expanded',String(active));}
  for(const panel of document.querySelectorAll('[data-settings-panel]'))panel.hidden=panel.dataset.settingsPanel!==name;
  $('settings-save').hidden=!name;
}
function updateSettingsSummary(){
  const base=$('browse-seconds').value,extra=$('browse-extra').value,limit=$('max-tasks').value;
  $('settings-summary').textContent=base&&extra&&limit?`浏览${base}秒 + 额外${extra}秒 · 本轮最多${limit}次`:'请完善执行设置';
  $('entry-mode-note').hidden=document.querySelector('input[name=mode]:checked').value==='auto';
}
function validateSettingsForm(){
  for(const name of ['distance','duration','interval','burst']){const low=$('swipe-'+name+'-min'),high=$('swipe-'+name+'-max');high.setCustomValidity(low.value&&high.value&&Number(low.value)>Number(high.value)?'最大值不能小于最小值':'');}
  const invalid=$('settings-form').querySelector(':invalid');
  if(invalid){const panel=invalid.closest('[data-settings-panel]');if(panel)showSettings(panel.dataset.settingsPanel);invalid.focus();}
  return $('settings-form').reportValidity();
}
async function action(name, extra={}){
  if(requesting)return;
  if(!state&&name!=='shutdown'){toast('正在读取当前设备，请稍候');return;}
  if(['run','connect','configure'].includes(name)&&!validateSettingsForm())return;
  if(name==='run'&&!config().enabled_task_kinds.length){showSettings('tasks');toast('请至少选择一种本轮任务类型');return;}
  if(name==='run'&&instanceProblem()){toast(instanceProblem());return;}
  requesting=true;
  try{let connection;
    if(name==='connect'){connection={device_type:$('device-type').value,serial:$('serial').value.trim()};const target=await MiaoDevices.prepare();if(!state||state.workspace_id!==target||MiaoAPI.device!==target)throw new Error('设备已切换，请重新连接');$('serial').value=connection.serial;$('device-type').value=connection.device_type;}
    const payload={app:state?.app,activity:state?.activity,...extra,auto_open:document.querySelector('input[name=mode]:checked').value==='auto'};
    if(['run','connect','configure'].includes(name))payload.config=config();
    if(name==='run'){payload.all_instances=clonesEnabled;payload.activities=MiaoActivities.plan;if(!payload.activities.length)throw new Error('请先勾选至少一个活动');const queued=clonesEnabled&&$('sequence-instances').checked;if(payload.activities.length===1){payload.instance_ids=queued?queueOrder:[state.selected_instance];if(!payload.instance_ids.length||payload.instance_ids.some(id=>!id||!state.app_instances.some(item=>item.id===id)))throw new Error('所选应用实例已失效，请重新选择');}if(queued)payload.auto_open=true;}
    if(name==='connect')Object.assign(payload,connection);
    const response=await fetch(MiaoAPI.url('/api/'+name),{method:'POST',headers:{'Content-Type':'application/json','X-Miao-Token':token},body:JSON.stringify(payload)});
    const result=await response.json();if(!response.ok)throw new Error(result.error||'操作失败');if(name==='shutdown'){toast('助手已退出，可以关闭此页面');return;}if(name==='configure')toast('执行设置已应用');await poll();
  }catch(error){toast(error.message);}finally{requesting=false;}
}
async function discover(){
  $('discover').disabled=true;
  try{const response=await fetch(MiaoAPI.url('/api/inventory'));if(!response.ok)throw new Error('设备检测失败');const data=await response.json();const options=$('device-options');options.replaceChildren();
    for(const d of data.devices){const o=el('option','',`${d.model||d.type} · ${d.status}`);o.value=d.serial;options.append(o);}
    const ready=data.devices.filter(d=>d.status==='device');
    if(ready.length===1&&!$('serial').value){$('serial').value=ready[0].serial;$('device-type').value=ready[0].type;}
    $('device-hint').textContent=data.devices.length?data.devices.map(d=>`${d.model||d.serial} · ${{device:'已连接',unauthorized:'请在手机允许 USB 调试',offline:'设备离线'}[d.status]||d.status}`).join('；'):(data.errors.join('；')||'暂未检测到设备。请检查数据线和 USB 调试，也可填入已连接的无线调试地址。');
  }catch(e){toast(e.message);}finally{$('discover').disabled=false;}
}
function renderInstances(busy){
  const instances=state.app_instances||[], hash=JSON.stringify(instances);
  $('toggle-instances').disabled=busy;$('instance-panel').hidden=false;$('toggle-instances').setAttribute('aria-expanded',String(clonesEnabled));$('toggle-instances').classList.toggle('is-open',clonesEnabled);
  const target=instances.find(i=>i.id===state.selected_instance);
  const queue=state.instance_queue||[],position=queue.findIndex(i=>i.status==='running');
  $('instance-summary').textContent=(target?(busy?'执行中：':'已选：')+target.label:state.connected?'应用实例尚未确认':'连接后确认应用实例')+(busy&&queue.length>1?` · 队列${position+1}/${queue.length}`:'');
  if(hash!==instanceHash){instanceHash=hash;const select=$('app-instance');select.replaceChildren();const placeholder=el('option','',instances.length?'请选择要执行的应用实例':'未发现可用应用实例');placeholder.value='';placeholder.disabled=true;select.append(placeholder);for(const item of instances){const option=el('option','',item.label);option.value=item.id;select.append(option);}queueOrder=clonesEnabled?instances.map(i=>i.id):queueOrder.filter(id=>instances.some(i=>i.id===id));if(!queueOrder.length&&instances.some(item=>item.id===state.selected_instance))queueOrder=[state.selected_instance];renderInstanceOptions(busy);}
  $('app-instance').value=state.selected_instance||'';$('app-instance').disabled=busy||!instances.length;
  $('refresh-instances').disabled=busy||!state.connected;$('sequence-instances').disabled=busy;
  $('instance-hint').textContent=(state.instance_warnings||[]).join('；')||'运行期间锁定所选实例；统计、失败记录与不可用应用分别保存，不读取账号信息。';
  for(const element of $('instance-options').querySelectorAll('input,button'))element.disabled=busy||element.dataset.blocked==='true';
  const results=$('instance-results');results.replaceChildren();const labels={pending:'等待执行',running:'执行中',finished:'本轮已结束',stopped:'已停止',needs_attention:'需要处理，队列已暂停',error:'异常，队列已停止'};
  for(const item of state.instance_queue||[]){const line=el('div','instance-result '+item.status);line.append(el('span','',item.label),el('span','',`${labels[item.status]||item.status} · 核验 ${item.stats?.completed||0} 次`));results.append(line);}
}
function renderInstanceOptions(busy=false){
  const list=$('instance-options');list.replaceChildren();const instances=state?.app_instances||[], ordered=[...queueOrder.map(id=>instances.find(i=>i.id===id)).filter(Boolean),...instances.filter(i=>!queueOrder.includes(i.id))];
  for(const item of ordered){const row=el('div','instance-option'),label=el('label'),check=el('input');check.type='checkbox';check.checked=queueOrder.includes(item.id);check.disabled=busy;check.addEventListener('change',()=>{queueOrder=check.checked?[...queueOrder,item.id]:queueOrder.filter(id=>id!==item.id);renderInstanceOptions();});label.append(check,document.createTextNode(item.label));row.append(label);
    for(const [delta,text] of [[-1,'↑'],[1,'↓']]){const button=el('button','text-button',text);button.type='button';button.setAttribute('aria-label',`${item.label} ${delta<0?'提前':'延后'}`);const index=queueOrder.indexOf(item.id);button.dataset.blocked=String(index<0||index+delta<0||index+delta>=queueOrder.length);button.disabled=busy||button.dataset.blocked==='true';button.addEventListener('click',()=>{const next=index+delta;[queueOrder[index],queueOrder[next]]=[queueOrder[next],queueOrder[index]];renderInstanceOptions();});row.append(button);}list.append(row);
  }
}
function renderTasks(force=false){
  const tasks=state.tasks||[];const hash=JSON.stringify(tasks)+filter+state.status+state.busy+state.task_scope;if(!force&&hash===taskHash)return;taskHash=hash;$('task-count').textContent=tasks.length;
  const list=tasks.filter(t=>filter==='all'||(filter==='manual'?['manual','failed','disabled'].includes(t.status):Boolean(t.enabled)));
  const table=$('task-table');table.replaceChildren();
  if(!list.length){const box=el('div','empty-state');box.append(el('div','empty-icon','☷'),el('h3','',tasks.length?'这个分类暂时没有任务':'任务还没入场'),el('p','',tasks.length?'切换其他分类查看执行情况。':'连接设备后点击「开始自动执行」，会先识别当前任务。'));table.append(box);return;}
  for(const t of list){const row=el('div','task-row');const info=el('div');const title=el('div','task-title');title.append(el('span','task-type',kindLabels[t.kind]||t.kind),el('span','',t.title));info.append(title,el('div','task-detail',t.reason||t.detail||'按页面要求浏览并返回'));const progress=el('div','task-progress',`${t.current}/${t.total}`),buttons=el('div','task-actions');
    const scope=state.task_scope,device=MiaoAPI.device,busy=busyStates.has(state.status)||state.busy;
    for(const [name,label] of [['task_skip','跳过'],['task_execute','执行']]){const button=el('button','task-choice',label);button.type='button';button.classList.toggle('is-selected',name==='task_execute'?Boolean(t.enabled):!t.enabled);button.setAttribute('aria-pressed',String(name==='task_execute'?Boolean(t.enabled):!t.enabled));button.disabled=!state.connected||!t.task_id||!t.controllable||t.status==='done'||t.current>=t.total||busy&&!['running','paused'].includes(state.status);button.title=name==='task_skip'?'今天跳过此任务，当前步骤先安全结束':'加入可执行队列，按正常流程执行；重置今日失败次数';button.addEventListener('click',async()=>{button.disabled=true;try{await MiaoAPI.request('/api/'+name,{method:'POST',token,payload:{task_scope:scope,task_id:t.task_id},device});if(device===MiaoAPI.device){toast(name==='task_skip'?'已记录今日跳过':'已加入可执行队列，不会立即开始操作');await poll();}}catch(error){toast(error.message);taskHash='';await poll();}});buttons.append(button);}
    row.append(info,progress,buttons,el('span','task-status '+t.status,taskLabels[t.status]||t.status));table.append(row);}
}
function renderLogs(){const logs=state.logs||[];const hash=logs.length?String(logs[logs.length-1].id):'';if(hash===logsHash)return;logsHash=hash;const list=$('log-list');const atBottom=list.scrollTop+list.clientHeight>=list.scrollHeight-30;list.replaceChildren();for(const log of logs){const line=el('div','log-line '+log.level);line.append(el('time','',log.time.slice(11,19)),el('span','log-level',log.level),el('span','log-text',log.message));list.append(line);}if(atBottom)list.scrollTop=list.scrollHeight;}
function render(){for(const a of document.querySelectorAll('a[href^="/api/report"],a[href^="/api/diagnostics"]'))a.href=MiaoAPI.url(a.pathname);
  window.dispatchEvent(new CustomEvent('miaotask:state',{detail:state}));
  const hasActivities=Boolean(state.catalog?.some(app=>app.activities.length)), hasPlan=Boolean(MiaoActivities.plan.length);
  $('app-version').textContent=state.version||'';
  if(!initialized){
    if(state.connected&&state.device?.serial){$('serial').value=state.device.serial;$('device-type').value=state.device.type;$('device-hint').textContent='当前工作区已连接 · '+state.device.serial;}
    const pref=deviceUIPreferences.get(MiaoAPI.device);
    clonesEnabled=Boolean(state.all_instances);
    queueOrder=pref?.queueOrder?.slice()||[];$('sequence-instances').checked=clonesEnabled;
    document.querySelector('input[name=mode][value="'+(pref?.mode||'auto')+'"]').checked=true;
  }
  const busy=busyStates.has(state.status)||state.busy||state.plugins_busy;renderInstances(busy);const running=state.status==='running',paused=state.status==='paused';
  const currentEntry=document.querySelector('input[name=mode][value=current]');currentEntry.disabled=busy||MiaoActivities.plan.length>1;if(MiaoActivities.plan.length>1)document.querySelector('input[name=mode][value=auto]').checked=true;
  $('connection-label').className='status-pill'+(state.connected?' connected':'');$('connection-label').replaceChildren(el('span'),document.createTextNode(statusLabels[state.status]||state.status));
  $('connect').disabled=busy||!hasActivities||!hasPlan;$('connect').textContent=state.connected?'重新连接 ↗':'连接设备 ↗';
  for(const id of ['run','capture'])$(id).disabled=!state.connected||busy;
  $('run').textContent=busy?'正在处理…':MiaoActivities.plan.length>1?'▶ 执行活动流水线':clonesEnabled&&$('sequence-instances').checked?'▶ 按顺序执行':'▶ 开始自动执行';if(!(state.app_instances||[]).length||!hasActivities||!hasPlan||MiaoActivities.locked)$('run').disabled=true;
  const instanceIssue=instanceProblem();$('instance-attention').hidden=!instanceIssue;$('instance-attention-message').textContent=instanceIssue;$('refresh-instances-visible').disabled=busy||requesting||!state.connected;$('choose-instance-visible').hidden=!(state.app_instances||[]).length;$('choose-instance-visible').disabled=busy||requesting;if(instanceIssue)$('run').disabled=true;
  $('pause').disabled=!running&&!paused;$('pause').textContent=paused?'▶ 继续':'Ⅱ 暂停';$('stop').disabled=!busy||state.status==='stopping';
  $('device-type').disabled=busy;$('serial').disabled=busy;
  $('save-settings').disabled=busy;
  $('common-settings-open').disabled=false;
  $('settings-device-label').textContent=(state.workspace_label||state.workspaces?.find(item=>item.id===MiaoAPI.device)?.label||'当前设备')+' · 所有已选活动共用';
  $('current-device-name').textContent=state.workspaces?.find(item=>item.id===MiaoAPI.device)?.label||'当前设备';
  $('phase-label').textContent=phaseLabels[state.phase]||'READY WHEN YOU ARE';
  $('execution-title').textContent=paused?'休息一下，进度还在':state.current_task||({disconnected:'准备好，再出发',ready:'手机已就绪',finished:'本轮任务已处理',needs_attention:'这一步，需要你来处理',error:'连接或执行遇到问题',stopped:'已停止执行'}[state.status]||statusLabels[state.status]);
  $('message').textContent=hasActivities?state.message:'框架已就绪，请打开活动中心从 GitHub 下载所需活动源码，勾选后连接设备。';$('countdown').hidden=!running||!state.remaining;$('remaining').textContent=state.remaining||0;
  document.querySelector('.execution-card').classList.toggle('is-active',running);document.querySelector('.execution-card').classList.toggle('needs-attention',['error','needs_attention'].includes(state.status));
  const round=state.stats||{},daily=state.daily,stats=daily?.stats||{};
  for(const [id,key] of [['completed','completed'],['claims','claims'],['cross','cross_app'],['skipped','skipped'],['failed','failed']])$('stat-'+id).textContent=stats[key]||0;
  $('daily-label').textContent=daily?`${daily.date} · ${state.app_instances?.find(i=>i.id===state.selected_instance)?.label||'当前设备'} · 今日累计`:'连接后读取本设备今日统计';
  $('daily-note').textContent=daily?`今日运行 ${clockText(stats.elapsed)} · 已确认不可用 ${daily.unavailable_count||0} 项 · 诊断记录 ${daily.failures_count||0} 条`:'同一设备、同一实例当天累计保存，重新开始不会清零';
  $('round-summary').textContent=`本轮 · 已尝试 ${round.attempts||0} 次 · 核验完成 ${round.completed||0} 次 · 领奖 ${round.claims||0} 次 · 未通过 ${round.failed||0} 项`;
  $('elapsed').textContent=`本轮运行 ${clockText(round.elapsed)}`;
  $('diagnostics').hidden=!daily;
  $('failure-folder').textContent=daily?.failures_dir||'连接后生成本设备当天的诊断目录';
  const failureTask=state.last_failure?.task;
  const failureTitle=typeof failureTask==='string'?failureTask:failureTask?.title;
  $('last-failure').textContent=state.last_failure?`最近记录：${state.last_failure.reason||'执行未通过'}${failureTitle?' · '+failureTitle:''}`:'遇到验证、恢复失败或核验失败时记录现场，原始截图仅保存在本机。';
  $('package-name').textContent=state.package||'设备未连接';$('preview-source').textContent=state.source==='ocr'?'本地 OCR 识别':state.source==='ui'?'页面控件识别':'手机截图预览';
  if(!initialized){const c={...config(),...(state.config||{})};for(const name of ['distance','duration','interval','burst'])for(const bound of ['min','max']){const value=Number(c['swipe_'+name+'_'+bound]);if(Number.isFinite(value))$('swipe-'+name+'-'+bound).value=Number((value*(name==='distance'?100:1)).toFixed(3));}$('browse-seconds').value=c.default_browse_seconds;$('browse-extra').value=c.browse_extra_seconds;$('max-attempts').value=c.max_attempts;$('max-tasks').value=c.max_tasks;$('cross-app').checked=c.allow_cross_app;$('ocr').checked=c.ocr;$('scroll').checked=c.scroll_browse;$('claim').checked=c.claim_rewards;for(const k of ['browse','search','visit','claim'])$('kind-'+k).checked=(c.enabled_task_kinds||['browse','search','visit','claim']).includes(k);initialized=true;}
  updateSettingsSummary();renderTasks();renderLogs();preview();MiaoDevices.refresh();
}
async function poll(){
  if(pollRequest)return pollRequest;
  const expectedDevice=MiaoAPI.device;pollRequest=(async()=>{try{const response=await fetch(MiaoAPI.url('/api/state',expectedDevice),{cache:'no-store',signal:AbortSignal.timeout(8000)});if(!response.ok)throw new Error('控制台连接失败');const next=await response.json();if(expectedDevice!==MiaoAPI.device)return;state=next;token=state.token;render();}catch(e){if(expectedDevice!==MiaoAPI.device)return;$('connection-label').replaceChildren(el('span'),document.createTextNode('服务已断开'));for(const id of ['run','connect','capture','pause','stop'])$(id).disabled=true;}})();
  try{await pollRequest;}finally{pollRequest=null;}
}
async function preview(){
  if(!state?.connected||document.hidden||$('preview-sidebar').hidden||previewRequest||!pageActive)return;const previewDevice=MiaoAPI.device;
  const sequence=state.image_sequence,updatedAt=state.image_updated_at;
  if(sequence!==undefined&&sequence===lastPreviewSequence)return;
  // The visible image is replaced only after the previous load finishes.
  // Rapid timers must not cancel an in-flight screenshot on slower devices.
  previewRequest=new Promise(resolve=>{
    const image=$('phone-image');
    const complete=ok=>{if(previewDevice!==MiaoAPI.device)ok=false;clearTimeout(timeout);image.removeEventListener('load',loaded);image.removeEventListener('error',failed);if(ok){lastPreviewSequence=sequence;previewReady=true;image.parentElement.style.aspectRatio=image.naturalWidth+'/'+image.naturalHeight;image.closest('.phone').classList.toggle('wide-screen',image.naturalWidth>image.naturalHeight);image.hidden=false;$('phone-placeholder').hidden=true;$('preview-time').textContent=new Date(updatedAt||Date.now()).toLocaleTimeString('zh-CN',{hour12:false});}else if(!previewReady){image.hidden=true;$('phone-placeholder').hidden=false;}resolve();};
    const loaded=()=>complete(true),failed=()=>complete(false);
    const timeout=setTimeout(()=>complete(false),10000);
    image.addEventListener('load',loaded,{once:true});image.addEventListener('error',failed,{once:true});
    image.src=MiaoAPI.url('/api/screenshot')+'&v='+(sequence??Date.now());
  });
  try{await previewRequest;}finally{previewRequest=null;}
}
async function pollLoop(){clearTimeout(pollTimer);if(!pageActive||polling)return;polling=true;try{await poll();}finally{polling=false;if(pageActive)pollTimer=setTimeout(pollLoop,document.hidden?5000:300);}}
async function previewLoop(){clearTimeout(previewTimer);if(!pageActive||document.hidden||previewing)return;previewing=true;try{await preview();}finally{previewing=false;if(pageActive&&!document.hidden)previewTimer=setTimeout(previewLoop,300);}}
document.addEventListener('visibilitychange',()=>{clearTimeout(pollTimer);clearTimeout(previewTimer);pollLoop();if(!document.hidden)previewLoop();});
window.addEventListener('pagehide',()=>{pageActive=false;clearTimeout(pollTimer);clearTimeout(previewTimer);});
window.addEventListener('pageshow',event=>{if(event.persisted){pageActive=true;pollLoop();previewLoop();}});
for(const id of ['connect','run','capture','stop'])$(id).addEventListener('click',()=>action(id));
MiaoDialogs.bind($('common-settings'));
$('common-settings-open').addEventListener('click',()=>showSettings(settingsSection||'tasks'));
for(const button of document.querySelectorAll('[data-open-settings]'))button.addEventListener('click',()=>{if(state)showSettings(settingsSection||'tasks');});
$('common-settings-close').addEventListener('click',()=>$('common-settings').close());
for(const button of document.querySelectorAll('[data-settings-toggle]'))button.addEventListener('click',()=>showSettings(button.dataset.settingsToggle));
$('toggle-instances').addEventListener('click',()=>{clonesEnabled=!clonesEnabled;if(!clonesEnabled){$('sequence-instances').checked=false;$('instance-options').hidden=true;queueOrder=state?.selected_instance&&defaultInstanceId()?[defaultInstanceId()]:[];renderInstanceOptions();const busy=state&&(busyStates.has(state.status)||state.busy);if(state?.connected&&!busy&&state.selected_instance&&state.selected_instance!==defaultInstanceId())action('select_instance',{instance_id:defaultInstanceId()});}if(state)render();else $('instance-panel').hidden=false;});
$('settings-form').addEventListener('input',()=>{for(const name of ['distance','duration','interval','burst'])$('swipe-'+name+'-max').setCustomValidity('');updateSettingsSummary();});
for(const input of document.querySelectorAll('input[name=mode]'))input.addEventListener('change',updateSettingsSummary);
$('app-instance').addEventListener('change',()=>{if(!$('sequence-instances').checked){queueOrder=[$('app-instance').value];renderInstanceOptions();}action('select_instance',{instance_id:$('app-instance').value});});
$('refresh-instances').addEventListener('click',()=>action('refresh_instances'));
$('refresh-instances-visible').addEventListener('click',()=>action('refresh_instances'));
$('choose-instance-visible').addEventListener('click',()=>document.querySelector('.device-tab.selected .device-edit')?.click());
$('sequence-instances').addEventListener('change',async()=>{clonesEnabled=$('sequence-instances').checked;queueOrder=clonesEnabled?(state?.app_instances||[]).map(i=>i.id):[defaultInstanceId()].filter(Boolean);if(!clonesEnabled&&defaultInstanceId()&&state?.selected_instance!==defaultInstanceId())await action('select_instance',{instance_id:defaultInstanceId()});await action('configure',{all_instances:clonesEnabled});if(state)render();});
$('pause').addEventListener('click',()=>action(state?.status==='paused'?'resume':'pause'));
$('save-settings').addEventListener('click',()=>action('configure'));

$('settings-form').addEventListener('submit',event=>event.preventDefault());
for(const button of document.querySelectorAll('[data-filter]'))button.addEventListener('click',()=>{filter=button.dataset.filter;document.querySelectorAll('[data-filter]').forEach(b=>b.classList.toggle('active',b===button));if(state)renderTasks(true);});
for(const name of ['tasks','logs'])$(name+'-tab').addEventListener('click',()=>{for(const other of ['tasks','logs']){const selected=name===other;$(other+'-tab').classList.toggle('active',selected);$(other+'-tab').setAttribute('aria-selected',String(selected));$(other+'-panel').hidden=!selected;}});
window.addEventListener('miaotask:activity-changed',()=>{initialized=false;taskHash=logsHash=instanceHash='';poll();});
window.addEventListener('miaotask:plan-changed',()=>{taskHash=instanceHash='';poll();});
window.addEventListener('miaotask:workspaces-changed',()=>poll());
window.addEventListener('miaotask:error',event=>toast(event.detail));
window.addEventListener('miaotask:device-selected',()=>{
  if($('common-settings').open)$('common-settings').close();$('common-settings-open').disabled=true;
  if(state?.workspace_id)deviceUIPreferences.set(state.workspace_id,{clonesEnabled,queueOrder:queueOrder.slice(),sequence:$('sequence-instances').checked,mode:document.querySelector('input[name=mode]:checked').value});
  initialized=false;taskHash=logsHash=instanceHash='';clonesEnabled=false;queueOrder=[];
  previewReady=false;lastPreviewSequence=null;$('phone-image').parentElement.style.aspectRatio='9/20';$('phone-image').closest('.phone').classList.remove('wide-screen');$('preview-time').textContent='—';$('phone-image').src='';$('phone-image').hidden=true;$('phone-placeholder').hidden=false;
  state=null;for(const id of ['run','connect','capture','pause','stop','refresh-instances-visible','choose-instance-visible'])$(id).disabled=true;$('instance-attention').hidden=true;$('serial').value='';$('device-hint').textContent='请连接当前工作区的设备';poll().then(poll);
});
window.addEventListener('miaotask:batch',async event=>{
  if(event.detail.action==='run'&&!validateSettingsForm())return;
  try{if(event.detail.action==='run'&&!MiaoActivities.plan.length)throw new Error('请先勾选至少一个活动');const result=await MiaoAPI.request('/api/workspaces/batch',{method:'POST',token,payload:{...event.detail,config:config(),auto_open:document.querySelector('input[name=mode]:checked').value==='auto',activities:MiaoActivities.plan}});
    const failed=result.results.filter(row=>!row.ok);toast(failed.length?failed.map(row=>row.error).join('；'):'所选设备已收到操作，各自独立执行');await poll();
  }catch(error){toast(error.message);}
});
MiaoAPI.request('/api/workspaces').then(data=>{if(data.devices?.length&&!data.devices.some(row=>row.id===MiaoAPI.device))MiaoAPI.selectDevice(data.devices[0].id);}).catch(()=>{}).finally(()=>poll().then(()=>{discover();previewLoop();pollLoop();}));
