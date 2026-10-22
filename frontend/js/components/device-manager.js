'use strict';
(() => {
  const byId=id=>document.getElementById(id);
  const labels={disconnected:'未连接',connecting:'连接中',ready:'就绪',running:'执行中',paused:'已暂停',scanning:'识别中',finished:'已结束',stopping:'停止中',stopped:'已停止',needs_attention:'需要处理',error:'异常'};
  let snapshot, checked=new Set(['default']), adding=false;
  const cards=new Map();
  function select(id){
    MiaoAPI.selectDevice(id);
    window.dispatchEvent(new CustomEvent('miaotask:device-selected',{detail:id}));
  }
  function render(){
    const rows=snapshot?.workspaces||[],known=new Set(rows.map(row=>row.id));
    for(const [id,card] of cards)if(!known.has(id)){card.remove();cards.delete(id);}
    for(const row of rows){
      let card=cards.get(row.id);
      if(!card){
        card=document.createElement('div');
        const check=document.createElement('input');check.type='checkbox';
        check.addEventListener('change',()=>{check.checked?checked.add(row.id):checked.delete(row.id);render();});
        const button=document.createElement('button');button.type='button';button.setAttribute('role','tab');
        button.addEventListener('click',()=>select(row.id));button.append(document.createElement('strong'),document.createElement('span'));
        card.append(check,button);cards.set(row.id,card);byId('device-tabs').append(card);
      }
      card.className='device-tab'+(row.id===MiaoAPI.device?' selected':'');card.dataset.status=row.status;
      const check=card.firstChild,button=card.lastChild;
      check.checked=checked.has(row.id);check.setAttribute('aria-label','批量选择 '+row.label);
      button.setAttribute('aria-selected',String(row.id===MiaoAPI.device));button.title=row.message;
      button.firstChild.textContent=row.label;
      button.lastChild.textContent=(labels[row.status]||row.status)+(row.display?` · ${row.display.width}×${row.display.height}`:'')+` · 今日完成 ${row.stats.completed||0}`;
    }
    byId('remove-device').disabled=rows.length<=1||snapshot?.busy||snapshot?.plugins_busy;
    byId('add-device').disabled=Boolean(snapshot?.plugins_busy);
    byId('batch-run').disabled=byId('batch-open-app').disabled=!checked.size||Boolean(snapshot?.plugins_busy);
    byId('batch-stop').disabled=!checked.size;
  }
  async function discover(emulators=false){
    byId('add-device-message').textContent='正在检测设备…';
    try{
      const data=await MiaoAPI.request(emulators?'/api/emulators':'/api/inventory',{timeout:30000});
      const select=byId('new-device-address');
      select.replaceChildren();const empty=document.createElement('option');empty.value='';empty.textContent='先添加，稍后连接';select.append(empty);
      for(const item of data.devices){const option=document.createElement('option');option.value=item.serial;option.dataset.type=item.type;option.textContent=`${item.model||item.type} · ${item.serial} · ${item.status}`;select.append(option);}
      byId('add-device-message').textContent=data.errors?.join('；')||`发现 ${data.devices.length} 台设备，请选择。模拟器不会自动启动或更改配置。`;
    }catch(error){byId('add-device-message').textContent=error.message;}
  }
  byId('add-device').addEventListener('click',()=>{byId('add-device-panel').hidden=!byId('add-device-panel').hidden;if(!byId('add-device-panel').hidden)discover();});
  byId('refresh-device-list').addEventListener('click',()=>discover());
  byId('find-emulators').addEventListener('click',()=>discover(true));
  byId('confirm-add-device').addEventListener('click',async()=>{
    if(adding||!snapshot)return;adding=true;byId('confirm-add-device').disabled=true;
    try{
      const choice=byId('new-device-address'),serial=choice.value,type=choice.selectedOptions[0]?.dataset.type||'uiautomator2';
      const result=await MiaoAPI.request('/api/workspaces',{method:'POST',token:snapshot.token,payload:{label:byId('new-device-name').value.trim()}});
      checked.add(result.id);select(result.id);byId('add-device-panel').hidden=true;
      if(serial)await MiaoAPI.request('/api/connect',{method:'POST',device:result.id,token:snapshot.token,payload:{serial,device_type:type}});
    }catch(error){window.dispatchEvent(new CustomEvent('miaotask:error',{detail:error.message}));}
    finally{adding=false;byId('confirm-add-device').disabled=false;}
  });
  byId('remove-device').addEventListener('click',async()=>{
    try{
      const id=MiaoAPI.device,result=await MiaoAPI.request('/api/workspaces/remove',{method:'POST',token:snapshot.token,payload:{id}});
      checked.delete(id);select(result.devices[0].id);
    }catch(error){window.dispatchEvent(new CustomEvent('miaotask:error',{detail:error.message}));}
  });
  for(const [id,action] of [['batch-run','run'],['batch-open-app','open_app'],['batch-stop','stop']])byId(id).addEventListener('click',()=>window.dispatchEvent(new CustomEvent('miaotask:batch',{detail:{devices:[...checked],action}})));
  window.addEventListener('miaotask:state',event=>{
    snapshot=event.detail;
    const known=new Set((snapshot.workspaces||[]).map(row=>row.id));checked=new Set([...checked].filter(id=>known.has(id)));
    render();
  });
})();
