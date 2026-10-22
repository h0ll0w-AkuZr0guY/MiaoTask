'use strict';
(() => {
  const $=id=>document.getElementById(id), dialog=$('activity-center');
  const node=(tag,cls,text)=>{const n=document.createElement(tag);if(cls)n.className=cls;if(text!==undefined)n.textContent=text;return n;};
  let snapshot=null, catalog=null, loading=false, polling=null, operation=false, listSignature='';
  const message=value=>{$('activity-center-message').textContent=value;};
  const operationText=value=>typeof value==='string'?value:value?.message||'';
  function render(){
    const locked=operation||Boolean(catalog?.busy||snapshot?.plugins_busy)||(snapshot?.workspaces||[]).some(row=>row.busy||['running','connecting','scanning','paused','stopping'].includes(row.status));
    $('activity-center-refresh').disabled=loading||locked;
    $('activity-source-label').textContent=catalog?.test_source?'本机测试活动源':'MiaoTask 官方活动源';
    $('activity-source-label').classList.toggle('local-test',Boolean(catalog?.test_source));
    if(locked&&!operation)message(operationText(catalog?.operation)||operationText(snapshot?.plugins_operation)||'设备正在操作中，请全部停止后再修改活动包。');
    const signature=JSON.stringify([catalog?.catalog,catalog?.installed,locked,loading&&!catalog]);
    if(signature===listSignature)return;listSignature=signature;
    const list=$('activity-center-list');list.replaceChildren();
    for(const app of catalog?.catalog||[]){const group=node('section','activity-source-group');group.append(node('h3','',app.label));const cards=node('div','activity-center-grid');
      for(const activity of app.activities||[]){const card=node('article','activity-download-card');card.append(node('h4','',activity.label),node('p','',`活动版本 ${activity.version} · ${activity.installed?'已安装 '+activity.installed_version:'尚未安装'}`));const actions=node('div','activity-download-actions');const button=node('button','feature-button',activity.installed?(activity.installed_version===activity.version?'重新安装':'更新活动'):'下载安装');button.type='button';button.disabled=locked||!activity.compatible;button.addEventListener('click',()=>operate('install',app.id,activity.id));actions.append(button);if(activity.installed){const remove=node('button','text-button','卸载');remove.type='button';remove.disabled=locked;remove.addEventListener('click',()=>operate('uninstall',app.id,activity.id));actions.append(remove);}card.append(actions);if(!activity.compatible)card.append(node('small','activity-incompatible','此活动需要其他版本的框架或运行环境，请先升级框架。'));cards.append(card);}
      group.append(cards);list.append(group);
    }
    if(!catalog?.catalog?.length)list.append(node('p','activity-center-empty',loading?'正在读取活动目录…':'暂时没有可下载活动，请检查网络或刷新目录。'));
    const visible=new Set((catalog?.packages||[]).map(item=>item.app+'/'+item.activity));
    for(const installed of catalog?.installed||[]){if(visible.has(installed.app+'/'+installed.activity))continue;const row=node('article','activity-download-card');row.append(node('h4','',installed.label||installed.activity),node('p','',`本地已安装 ${installed.version} · 暂未列入当前目录`));const remove=node('button','text-button','卸载');remove.type='button';remove.disabled=locked;remove.addEventListener('click',()=>operate('uninstall',installed.app,installed.activity));row.append(remove);list.append(row);}
  }
  async function load(refresh=false){
    if(loading)return;loading=true;if(refresh)message('正在更新活动目录…');render();
    try{catalog=await MiaoAPI.request('/api/plugins'+(refresh?'?refresh=1':''),{timeout:35000});message(catalog.error||operationText(catalog.operation)||(catalog.test_source?'测试目录仅来自本机。点击安装后才会下载并启用活动。':'只下载你选择的活动。活动包含代码，请确认来自官方审阅的发布。'));}
    catch(error){message(error.message);}
    finally{loading=false;render();if(dialog.open){clearTimeout(polling);polling=setTimeout(()=>load(),catalog?.busy||operation?700:2500);}}
  }
  async function operate(action,app,activity){
    if(operation||!snapshot)return;operation=true;message(action==='install'?'正在下载、校验并安装，请稍候…':'正在卸载活动，已有日记录会保留…');render();
    try{await MiaoAPI.request('/api/plugins/'+action,{method:'POST',token:snapshot.token,payload:{app,activity},timeout:35000});await load();window.dispatchEvent(new Event('miaotask:plan-changed'));}
    catch(error){message(error.message);}
    finally{operation=false;render();}
  }
  function open(){if(!dialog.open)dialog.showModal();load();}
  $('activity-center-open').addEventListener('click',open);$('activity-center-close').addEventListener('click',()=>dialog.close());$('activity-center-refresh').addEventListener('click',()=>load(true));
  dialog.addEventListener('close',()=>clearTimeout(polling));window.addEventListener('miaotask:open-activity-center',open);window.addEventListener('miaotask:state',event=>{snapshot=event.detail;if(dialog.open&&Boolean(catalog?.busy)!==Boolean(snapshot.plugins_busy))load();});
})();
