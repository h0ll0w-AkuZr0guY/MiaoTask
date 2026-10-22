'use strict';
(() => {
  const $=id=>document.getElementById(id), node=(tag,cls,text)=>{const n=document.createElement(tag);if(cls)n.className=cls;if(text!==undefined)n.textContent=text;return n;};
  let snapshot=null, viewApp='', workspace='', signature='';
  const pending=new Map();
  const key=item=>item.app+'/'+item.activity;
  const error=message=>window.dispatchEvent(new CustomEvent('miaotask:error',{detail:message}));
  function plan(){if(!snapshot||snapshot.workspace_id&&snapshot.workspace_id!==MiaoAPI.device)return [];return pending.get(MiaoAPI.device)?.activities || snapshot.activity_plan || (snapshot.app&&snapshot.activity?[{app:snapshot.app,activity:snapshot.activity}]:[]);}
  function busy(){return Boolean(snapshot?.busy||snapshot?.plugins_busy||pending.has(MiaoAPI.device)||['running','connecting','scanning','paused','stopping'].includes(snapshot?.status));}
  function label(item){return snapshot?.catalog?.find(app=>app.id===item.app)?.activities?.find(activity=>activity.id===item.activity)?.label||item.activity;}
  async function save(activities){
    if(!snapshot||busy())return;
    const device=MiaoAPI.device, token=snapshot.token;
    pending.set(device,{activities:activities.map(item=>({app:item.app,activity:item.activity})),since:Date.now()});signature='';render();
    try{await MiaoAPI.request('/api/select_pipeline',{method:'POST',token,device,payload:{activities}});window.dispatchEvent(new Event('miaotask:plan-changed'));}
    catch(cause){pending.delete(device);signature='';render();error(cause.message);}
  }
  function update(item,enabled){const next=plan().filter(other=>key(other)!==key(item));if(enabled)next.push(item);if(next.length>16){error('每台设备最多选择16个活动');render();return;}save(next);}
  function render(){
    if(!snapshot)return;
    const catalog=snapshot.catalog||[], activities=plan(), locked=busy();
    if(!catalog.some(app=>app.id===viewApp))viewApp=catalog.find(app=>app.id===snapshot.app)?.id||catalog[0]?.id||'';
    const next=JSON.stringify([catalog,activities,viewApp,locked,snapshot.activity_pipeline,snapshot.app,snapshot.activity]);
    if(next===signature)return;signature=next;
    const tabs=$('application-nav');tabs.replaceChildren();
    for(const app of catalog){const button=node('button','application-tab'+(viewApp===app.id?' active':''));button.type='button';button.setAttribute('role','tab');button.setAttribute('aria-selected',String(viewApp===app.id));button.append(node('span','application-initial',app.label.slice(0,1)),node('strong','',app.label),node('small','',String(app.activities.length)));button.addEventListener('click',()=>{viewApp=app.id;signature='';render();});tabs.append(button);}
    if(!catalog.length)tabs.append(node('p','field-hint','框架已就绪，安装所需应用的活动后即可开始。'));
    const current=catalog.find(app=>app.id===viewApp), list=$('activity-tiles');list.replaceChildren();
    $('activity-selection-title').textContent=current?current.label+' · 已安装活动':'选择要执行的活动';
    for(const activity of current?.activities||[]){const item={app:current.id,activity:activity.id},selected=activities.some(other=>key(other)===key(item));const tile=node('label','activity-tile'+(selected?' selected':'')),check=node('input');check.type='checkbox';check.checked=selected;check.disabled=locked;check.addEventListener('change',()=>update(item,check.checked));const text=node('span');text.append(node('strong','',activity.label),node('small','',selected?'已加入本设备流水线':'勾选后按顺序执行'));tile.append(check,text);list.append(tile);}
    if(!current?.activities?.length){const empty=node('div','activity-empty');empty.append(node('strong','','按需安装，保持轻量'),node('p','','活动独立更新，框架无需为每个活动重新下载。'));const button=node('button','feature-button','打开活动中心');button.type='button';button.addEventListener('click',()=>window.dispatchEvent(new Event('miaotask:open-activity-center')));empty.append(button);list.append(empty);}
    const queue=$('activity-pipeline');queue.replaceChildren();
    const statuses={pending:'等待执行',running:'执行中',finished:'已结束',stopped:'已停止',needs_attention:'待本人处理',error:'出现异常'};
    const running=snapshot.activity_pipeline||[];
    activities.forEach((item,index)=>{const row=node('div','activity-pipeline-row'),description=node('span','activity-pipeline-label');description.append(node('small','pipeline-number',String(index+1)),node('strong','',label(item)),node('small','',catalog.find(app=>app.id===item.app)?.label||item.app));row.append(description);const result=running.find(other=>key(other)===key(item));if(result)row.append(node('span','pipeline-status '+result.status,statuses[result.status]||result.status));
      for(const [delta,text] of [[-1,'↑'],[1,'↓']]){const button=node('button','text-button',text);button.type='button';button.setAttribute('aria-label',label(item)+(delta<0?'提前':'延后'));button.disabled=locked||index+delta<0||index+delta>=activities.length;button.addEventListener('click',()=>{const reordered=activities.slice();[reordered[index],reordered[index+delta]]=[reordered[index+delta],reordered[index]];save(reordered);});row.append(button);}
      const remove=node('button','text-button','移除');remove.type='button';remove.disabled=locked;remove.addEventListener('click',()=>update(item,false));row.append(remove);queue.append(row);
    });
    if(!activities.length)queue.append(node('p','field-hint','尚未选择活动。勾选一个或多个活动后，点击下方开始；不会因勾选立即执行。'));
    $('pipeline-count').textContent=String(activities.length);$('pipeline-clear').disabled=locked||!activities.length;
    $('activity-name').textContent=activities.length>1?`${label(activities[0])} 等 ${activities.length} 个活动，按顺序执行`:activities.length?label(activities[0]):'请先选择活动';
    for(const [select,rows,value] of [[$('activity-app'),catalog,snapshot.app],[$('activity-choice'),catalog.find(app=>app.id===snapshot.app)?.activities||[],snapshot.activity]]){select.replaceChildren(...rows.map(row=>{const option=node('option','',row.label);option.value=row.id;return option;}));select.value=value||'';select.disabled=locked;}
    $('activity-apply').disabled=true;
  }
  $('pipeline-clear').addEventListener('click',()=>save([]));
  window.addEventListener('miaotask:device-selected',()=>{snapshot=null;signature='';for(const control of document.querySelectorAll('#activity-tiles input,#activity-pipeline button,#pipeline-clear'))control.disabled=true;});
  window.addEventListener('miaotask:state',event=>{snapshot=event.detail;const device=MiaoAPI.device;if(workspace!==device){workspace=device;viewApp=snapshot.app||'';signature='';}const change=pending.get(device);if(change&&(JSON.stringify(snapshot.activity_plan||[])===JSON.stringify(change.activities)||Date.now()-change.since>10000||snapshot.status==='error'))pending.delete(device);render();});
  window.MiaoActivities=Object.freeze({get plan(){return plan().map(item=>({...item}));},get locked(){return busy();}});
})();
