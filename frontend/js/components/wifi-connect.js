'use strict';
(() => {
  const byId = id => document.getElementById(id);
  const status = byId('wifi-status');
  let snapshot, requesting = false, finding = false, reflectedSerial = '';
  const ids = ['wifi-pair','wifi-connect','wifi-discover','wifi-pair-address','wifi-connect-address','wifi-code'];
  const addresses = {}, drafts = new Map();
  let pendingPair = '';
  // Keep raw endpoints for IPv6 and pasted discovery values; IPv4 is the default.
  for (const kind of ['pair','connect']) {
    const raw=byId('wifi-'+kind+'-address'), container=document.createElement('div');
    const caption=document.createElement('span');caption.textContent=raw.parentElement.firstChild.textContent;
    const options=byId('wifi-'+kind+'-options');
    raw.parentElement.replaceWith(container);container.className='address-field';container.append(caption);
    const group=document.createElement('div');group.className='ipv4-fields';group.setAttribute('role','group');group.setAttribute('aria-label',kind==='pair'?'配对地址':'连接地址');
    const fields=[];
    for(let i=0;i<5;i++){
      if(i){const separator=document.createElement('span');separator.textContent=i===4?':':'.';separator.setAttribute('aria-hidden','true');group.append(separator);}
      const input=document.createElement('input');input.type='text';input.inputMode='numeric';input.autocomplete='off';input.maxLength=i===4?5:3;input.placeholder=i===4?'端口':'0';input.setAttribute('aria-label',(kind==='pair'?'配对':'连接')+(i===4?'端口':' IP 第'+(i+1)+'段'));input.className=i===4?'address-port':'address-octet';group.append(input);fields.push(input);
      input.addEventListener('input',()=>{input.value=input.value.replace(/[^0-9]/g,'');if(i<4&&input.value.length===3){fields[i+1]?.focus();fields[i+1]?.select();}});
      input.addEventListener('keydown',event=>{if(event.key==='Backspace'&&!input.value&&i>0){event.preventDefault();const previous=fields[i-1];previous.value=previous.value.slice(0,-1);previous.focus();}if((event.key==='.'||event.key===':')&&i<4){event.preventDefault();fields[i+1].focus();fields[i+1].select();}if(event.key==='Enter'){event.preventDefault();submit(kind==='pair');}});
      input.addEventListener('paste',event=>{const value=event.clipboardData.getData('text').trim();if(/^\d+\.\d+\.\d+\.\d+(?::\d+)?$/.test(value)){event.preventDefault();setAddress(kind,value);}});
    }
    const advanced=document.createElement('div');advanced.append(raw,options);container.append(group,advanced);advanced.hidden=true;
    const choices=document.createElement('div');choices.className='address-choices';container.append(choices);
    raw.addEventListener('change',()=>{const match=/^(\d+)\.(\d+)\.(\d+)\.(\d+):(\d+)$/.exec(raw.value.trim());if(match)fields.forEach((field,i)=>field.value=match[i+1]);});
    addresses[kind]={raw,fields,advanced,choices,group};
  }
  function addressMode(full){
    for(const editor of Object.values(addresses)){
      if(full&&!editor.advanced.open&&editor.fields.every(field=>field.value))editor.raw.value=editor.fields.slice(0,4).map(field=>field.value).join('.')+':'+editor.fields[4].value;
      if(!full){const match=/^(\d+)\.(\d+)\.(\d+)\.(\d+):(\d+)$/.exec(editor.raw.value.trim());if(match)editor.fields.forEach((field,i)=>field.value=match[i+1]);}
      editor.advanced.open=full;editor.advanced.hidden=!full;editor.group.hidden=full;
    }
  }
  byId('wifi-full-address').addEventListener('change',()=>addressMode(byId('wifi-full-address').checked));
  function setAddress(kind,value,ipOnly=false){
    const editor=addresses[kind],match=/^(\d+)\.(\d+)\.(\d+)\.(\d+)(?::(\d+))?$/.exec(value||'');
    editor.raw.value=ipOnly?(match?match.slice(1,5).join('.')+':'+editor.fields[4].value:(value||'').replace(/:\d+$/,':')):value||'';if(value&&!match){byId('wifi-full-address').checked=true;addressMode(true);}
    editor.fields.forEach((field,i)=>{if(!ipOnly||i<4)field.value=match?.[i+1]||'';});
  }
  function getAddress(kind){
    const editor=addresses[kind];if(editor.advanced.open)return editor.raw.value.trim();
    const values=editor.fields.map(field=>field.value);
    if(values.some((value,i)=>!/^\d+$/.test(value)||Number(value)>(i===4?65535:255)||i===4&&Number(value)<1))throw new Error('请填写四段有效 IP 和 1～65535 的端口。');
    return values.slice(0,4).map(Number).join('.')+':'+Number(values[4]);
  }
  function render() {
    const busy = !snapshot || requesting || window.MiaoDevices?.preparing || !window.MiaoDevices?.creating&&(snapshot?.busy || ['running','connecting','scanning','paused','stopping'].includes(snapshot?.status));
    for (const id of ids) byId(id).disabled = Boolean(busy || id === 'wifi-discover' && finding);
    for(const editor of Object.values(addresses))for(const field of [...editor.fields,...editor.choices.querySelectorAll('button')])field.disabled=Boolean(busy);
  }
  function mode(first) {
    byId('wifi-pair-panel').hidden = !first;
    byId('wifi-connect-title').textContent = first ? '2. 连接手机' : '连接已配对手机';
    for (const [id, active] of [['wifi-first',first],['wifi-known',!first]]) {
      byId(id).classList.toggle('is-open', active);
      byId(id).setAttribute('aria-pressed', String(active));
    }
    if (!first) byId('wifi-code').value = '';
  }
  async function submit(pair) {
    if (requesting) return;
    let payload;
    try { if (pair) {
      const address = getAddress('pair'), code = byId('wifi-code').value.trim();
      if (!address || !/^[0-9]{6}$/.test(code)) { status.textContent = '请填写配对弹窗中的 IP:端口和六位配对码。'; return; }
      payload = {pair_address:address, pair_code:code};
    } else {
      const address = getAddress('connect');
      if (!address) { status.textContent = '请填写无线调试主页面的 IP:端口。'; return; }
      payload = {connect_address:address};
    }
    } catch(error){status.textContent=error.message;return;}
    let expectedDevice=MiaoAPI.device;
    requesting = true; render();
    try {
      const target=await MiaoDevices.prepare();expectedDevice=target;
      if(MiaoAPI.device!==target||snapshot?.workspace_id!==target)throw new Error('设备已切换，请重新提交连接');
      await MiaoAPI.request('/api/wifi/' + (pair ? 'pair' : 'connect'), {
        method:'POST', token:snapshot?.token, payload, device:expectedDevice,
      });
      if(expectedDevice!==MiaoAPI.device)return;
      if(pair)pendingPair=payload.pair_address;
      status.textContent = pair ? '正在配对，请保持手机的配对码弹窗打开…' : '正在连接并读取手机实例…';
      window.dispatchEvent(new Event('miaotask:activity-changed'));
    } catch (error) { if(expectedDevice===MiaoAPI.device)status.textContent = error.message; }
    finally { if(expectedDevice===MiaoAPI.device)byId('wifi-code').value = ''; delete payload.pair_code; requesting = false; render(); }
  }
  byId('wifi-first').addEventListener('click', () => mode(true));
  byId('wifi-known').addEventListener('click', () => mode(false));
  byId('wifi-pair').addEventListener('click', () => submit(true));
  byId('wifi-connect').addEventListener('click', () => submit(false));
  byId('wifi-code').addEventListener('keydown', event => {
    if (event.key === 'Enter') { event.preventDefault(); submit(true); }
  });
  byId('wifi-connect-address').addEventListener('keydown', event => {
    if (event.key === 'Enter') { event.preventDefault(); submit(false); }
  });
  byId('wifi-discover').addEventListener('click', async () => {
    const expectedDevice=MiaoAPI.device;
    finding = true; render(); status.textContent = '正在查找无线调试广播…';
    try {
      const result = await MiaoAPI.request('/api/wifi/devices', {timeout:20000});
      if(expectedDevice!==MiaoAPI.device)return;
      for (const kind of ['pair','connect']) {
        const found = new Set((result.services || []).filter(item => item.kind === (kind === 'pair' ? 'pairing' : kind)).map(item => item.address));
        if (kind === 'connect') for (const item of result.devices || []) if (/^(?:[0-9.]+|\[[0-9a-fA-F:]+\]):[0-9]+$/.test(item.serial)) found.add(item.serial);
        addresses[kind].choices.replaceChildren(...[...found].map(address=>{const button=document.createElement('button');button.type='button';button.className='text-button';button.textContent=address;button.addEventListener('click',()=>setAddress(kind,address));return button;}));
        byId('wifi-'+kind+'-options').replaceChildren(...[...found].map(address => {
          const option = document.createElement('option'); option.value = address; return option;
        }));
      }
      status.textContent = result.services?.length || result.devices?.length
        ? '已发现无线地址，点击地址即可填入；请先与自己手机显示的地址核对。'
        : (result.errors || []).join('；') || '没有收到无线广播，请手动填写手机显示的地址；同一 Wi-Fi 仍可能有设备隔离。';
    } catch (error) { if(expectedDevice===MiaoAPI.device)status.textContent = error.message; }
    finally { finding = false; render(); }
  });
  window.addEventListener('miaotask:state', event => {
    snapshot = event.detail; render();
    if(pendingPair&&snapshot.wifi_paired&&!snapshot.busy&&snapshot.phase!=='connecting'&&snapshot.phase!=='connect_failed'){setAddress('connect',pendingPair,true);pendingPair='';(addresses.connect.advanced.open?addresses.connect.raw:addresses.connect.fields[4]).focus();}
    if (snapshot.phase === 'connecting' || snapshot.phase === 'connect_failed' || snapshot.wifi_paired && snapshot.status === 'disconnected') status.textContent = snapshot.message;
    if (!window.MiaoDevices?.creating && snapshot.connected && snapshot.status === 'ready' && snapshot.device?.serial?.includes(':')) {
      status.textContent = '无线设备已就绪，可以开始自动执行。日统计与同一手机的 USB 连接共用。';
      if (reflectedSerial !== snapshot.device.serial) {
        reflectedSerial = snapshot.device.serial;
        setAddress('connect',snapshot.device.serial);
        byId('serial').value = snapshot.device.serial;
        byId('device-type').value = 'uiautomator2';
      }
    }
    if (!snapshot.connected) reflectedSerial = '';
  });
  window.addEventListener('miaotask:device-selected',()=>{
    if(snapshot?.workspace_id)drafts.set(snapshot.workspace_id,Object.fromEntries(Object.entries(addresses).map(([kind,editor])=>[kind,{values:editor.fields.map(field=>field.value),raw:editor.raw.value,advanced:editor.advanced.open}])));
    snapshot=undefined;reflectedSerial='';pendingPair='';render();byId('wifi-code').value='';
    const saved=drafts.get(MiaoAPI.device);
    for(const [kind,editor] of Object.entries(addresses)){editor.fields.forEach((field,i)=>field.value=saved?.[kind]?.values[i]||'');editor.raw.value=saved?.[kind]?.raw||'';editor.advanced.open=saved?.[kind]?.advanced||false;editor.choices.replaceChildren();}
    byId('wifi-full-address').checked=Object.values(addresses).some(editor=>editor.advanced.open);addressMode(byId('wifi-full-address').checked);
    status.textContent='请为当前设备工作区填写连接信息。';
  });
  window.addEventListener('miaotask:editor-mode',event=>{
    if(event.detail.creating){byId('wifi-full-address').checked=false;addressMode(false);for(const kind of ['pair','connect'])setAddress(kind,'');byId('wifi-code').value='';pendingPair='';status.textContent='在手机开启无线调试，填写配对或连接地址。';}
    render();
  });
})();
