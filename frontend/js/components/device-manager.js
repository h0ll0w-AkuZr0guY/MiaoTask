'use strict';
(() => {
  const $=id=>document.getElementById(id),dialog=$('add-device-panel');
  const labels={disconnected:'未连接',connecting:'连接中',ready:'已连接',running:'执行中',paused:'已暂停',scanning:'识别中',finished:'已结束',stopping:'停止中',stopped:'已停止',needs_attention:'待处理',error:'异常'};
  const busy=row=>Boolean(row?.busy||['running','connecting','scanning','paused','stopping'].includes(row?.status));
  let snapshot,checked=new Set(['default']),editorId=null,creating=false,preparing=false,wanted='',discovery=0,method='usb';
  const cards=new Map(),drafts=new Map(),seenConnected=new Set();
  MiaoDialogs.bind(dialog);
  const error=message=>window.dispatchEvent(new CustomEvent('miaotask:error',{detail:message}));
  function select(id){if(MiaoAPI.device===id)return;MiaoAPI.selectDevice(id);window.dispatchEvent(new CustomEvent('miaotask:device-selected',{detail:id}));}
  function connectionMode(value){
    method=value;$('wired-connection').hidden=value==='wifi';$('device-hint').hidden=value==='wifi';
    $('wifi-manager').hidden=value!=='wifi';$('wifi-manager').open=value==='wifi';
    for(const button of dialog.querySelectorAll('[data-connection-mode]'))button.setAttribute('aria-pressed',String(button.dataset.connectionMode===value));
  }
  function saveDraft(){if(editorId)drafts.set(editorId,{serial:$('serial').value,type:$('device-type').value});}
  function openEditor(id){
    editorId=id;creating=!id;wanted='';
    const row=snapshot?.workspaces?.find(item=>item.id===id),draft=drafts.get(id);
    $('new-device-name').value=row?.label||'';
    $('serial').value=creating?'':snapshot?.device?.serial||draft?.serial||'';
    $('device-type').value=creating?'uiautomator2':snapshot?.device?.type||draft?.type||'uiautomator2';
    $('add-device-title').textContent=creating?'添加设备':'设备连接';
    $('confirm-add-device').textContent=creating?'仅创建设备':'保存名称';
    $('add-device-message').textContent='';
    connectionMode(!creating&&$('serial').value.includes(':')?'wifi':'usb');
    if(!dialog.open)dialog.showModal();
    window.dispatchEvent(new CustomEvent('miaotask:editor-mode',{detail:{creating}}));
    render();discover();
  }
  function render(){
    const rows=snapshot?.workspaces||[],known=new Set(rows.map(row=>row.id));
    for(const [id,card] of cards)if(!known.has(id)){card.remove();cards.delete(id);seenConnected.delete(id);}
    for(const row of rows){
      let card=cards.get(row.id);
      if(!card){
        card=document.createElement('div');
        const check=document.createElement('input');check.type='checkbox';check.addEventListener('change',()=>{check.checked?checked.add(row.id):checked.delete(row.id);render();});
        const button=document.createElement('button');button.type='button';button.setAttribute('role','tab');button.addEventListener('click',()=>select(row.id));button.append(document.createElement('strong'),document.createElement('span'));
        const edit=document.createElement('button');edit.type='button';edit.className='device-edit';edit.textContent='⚙';edit.addEventListener('click',()=>{if(row.id===MiaoAPI.device)openEditor(row.id);else{wanted=row.id;select(row.id);}});
        card.append(check,button,edit);cards.set(row.id,card);$('device-tabs').append(card);
      }
      if(row.connected)seenConnected.add(row.id);
      const lost=Boolean(row.connection_issue)||!row.connected&&(seenConnected.has(row.id)||row.status==='error');
      card.className='device-tab'+(row.id===MiaoAPI.device?' selected':'');card.dataset.status=row.status;
      card.dataset.connection=row.status==='connecting'?'connecting':lost?'lost':row.connected?'connected':'idle';
      const [check,button,edit]=card.children;
      check.checked=checked.has(row.id);check.setAttribute('aria-label','批量选择 '+row.label);
      button.setAttribute('aria-selected',String(row.id===MiaoAPI.device));button.title=row.message;button.firstChild.textContent=row.label;
      button.lastChild.textContent=(lost?'连接异常':labels[row.status]||row.status)+` · 完成 ${row.stats.completed||0}`;
      edit.setAttribute('aria-label','设置 '+row.label);edit.title='名称与连接';
    }
    $('device-count').textContent=rows.length+' / 8';$('batch-selection').textContent='已选 '+checked.size+' 台设备';
    $('add-device').disabled=preparing||Boolean(snapshot?.plugins_busy)||rows.length>=8;
    $('batch-run').disabled=$('batch-open-app').disabled=!checked.size||Boolean(snapshot?.plugins_busy);
    $('batch-stop').disabled=!checked.size;
    if(dialog.open){
      $('instance-panel').hidden=creating;
      const current=rows.find(row=>row.id===editorId),locked=preparing||!snapshot||!creating&&busy(current);
      $('device-editor-context').textContent=creating?'填好连接信息，直接连接即可创建':(current?.label||'当前设备')+' · 名称与连接';
      $('confirm-add-device').disabled=preparing||!snapshot;
      $('new-device-name').disabled=preparing;
      $('connect').disabled=locked||Boolean(snapshot?.plugins_busy);$('serial').disabled=$('device-type').disabled=locked;
      $('connect').textContent=creating?'创建并连接 ↗':current?.connected?'重新连接 ↗':'连接设备 ↗';
      $('remove-device').hidden=creating;$('remove-device').disabled=locked||rows.length<=1||Boolean(snapshot?.plugins_busy);
      $('device-editor-state').textContent=preparing?'正在保存设备…':creating?'未连接 · 支持 USB、模拟器与无线调试':(labels[current?.status]||'未连接');
    }
  }
  async function discover(emulators=false){
    const ticket=++discovery;$('add-device-message').textContent='正在检测设备…';
    try{
      const data=await MiaoAPI.request(emulators?'/api/emulators':'/api/inventory',{timeout:30000});
      if(ticket!==discovery||!dialog.open)return;
      const choice=$('new-device-address'),empty=document.createElement('option');empty.value='';empty.textContent='选择设备或填写下方地址';choice.replaceChildren(empty);
      for(const item of data.devices){const option=document.createElement('option');option.value=item.serial;option.dataset.type=item.type;option.textContent=`${item.model||item.type} · ${item.serial} · ${item.status}`;choice.append(option);}
      $('add-device-message').textContent=data.errors?.join('；')||`发现 ${data.devices.length} 台设备，请选择自己的设备。`;
    }catch(cause){if(ticket===discovery&&dialog.open)$('add-device-message').textContent=cause.message;}
  }
  function waitForWorkspace(id){
    if(snapshot?.workspace_id===id)return Promise.resolve();
    return new Promise((resolve,reject)=>{const timer=setTimeout(()=>{window.removeEventListener('miaotask:state',receive);reject(new Error('读取新设备超时，请重新打开连接设置'));},10000);
      function receive(event){if(event.detail.workspace_id===id){clearTimeout(timer);window.removeEventListener('miaotask:state',receive);resolve();}}
      window.addEventListener('miaotask:state',receive);
    });
  }
  async function prepare(){
    if(!dialog.open)return MiaoAPI.device;
    if(preparing)throw new Error('正在保存设备，请稍候');
    if(!snapshot)throw new Error('请等待设备列表读取完成');
    const source=MiaoAPI.device,label=$('new-device-name').value.trim(),wasCreating=creating;
    if(!wasCreating&&!label)throw new Error('请输入设备名称');
    preparing=true;render();
    try{
      if(wasCreating){
        const result=await MiaoAPI.request('/api/workspaces',{method:'POST',token:snapshot.token,payload:{label}});
        if(MiaoAPI.device!==source)throw new Error('当前设备已切换，请重新打开设置');
        editorId=result.id;creating=false;checked.add(result.id);
        const waiting=waitForWorkspace(result.id);select(result.id);await waiting;
        if(MiaoAPI.device!==editorId)throw new Error('当前设备已切换，未提交连接');
        $('new-device-name').value=result.devices.find(row=>row.id===editorId)?.label||label;
        $('add-device-title').textContent='设备连接';$('confirm-add-device').textContent='保存名称';
      }else{
        if(editorId!==source)throw new Error('设备已切换，请重新打开连接设置');
        await MiaoAPI.request('/api/workspaces/rename',{method:'POST',token:snapshot.token,payload:{id:editorId,label}});
      }
      window.dispatchEvent(new Event('miaotask:workspaces-changed'));return editorId;
    }finally{preparing=false;render();}
  }
  $('add-device').addEventListener('click',()=>openEditor(null));$('add-device-close').addEventListener('click',()=>dialog.close());
  dialog.addEventListener('close',()=>{saveDraft();discovery++;});
  $('discover').addEventListener('click',()=>discover());$('find-emulators').addEventListener('click',()=>discover(true));
  $('new-device-address').addEventListener('change',()=>{const option=$('new-device-address').selectedOptions[0];if(!option?.value)return;$('serial').value=option.value;$('device-type').value=option.dataset.type||'uiautomator2';connectionMode('usb');});
  for(const button of dialog.querySelectorAll('[data-connection-mode]'))button.addEventListener('click',()=>connectionMode(button.dataset.connectionMode));
  $('confirm-add-device').addEventListener('click',async()=>{try{await prepare();dialog.close();}catch(cause){$('add-device-message').textContent=cause.message;}});
  $('remove-device').addEventListener('click',async()=>{try{const id=editorId,result=await MiaoAPI.request('/api/workspaces/remove',{method:'POST',token:snapshot.token,payload:{id}});checked.delete(id);dialog.close();select(result.devices[0].id);}catch(cause){error(cause.message);}});
  for(const [id,action] of [['batch-run','run'],['batch-open-app','open_app'],['batch-stop','stop']])$(id).addEventListener('click',()=>window.dispatchEvent(new CustomEvent('miaotask:batch',{detail:{devices:[...checked],action}})));
  window.addEventListener('miaotask:device-selected',()=>{saveDraft();if(dialog.open&&editorId!==MiaoAPI.device)dialog.close();});
  window.addEventListener('miaotask:state',event=>{snapshot=event.detail;const known=new Set((snapshot.workspaces||[]).map(row=>row.id));checked=new Set([...checked].filter(id=>known.has(id)));render();if(wanted===snapshot.workspace_id)openEditor(wanted);});
  window.MiaoDevices=Object.freeze({prepare,refresh:render,get creating(){return creating&&dialog.open;},get preparing(){return preparing;}});
})();
