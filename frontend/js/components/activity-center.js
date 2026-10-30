'use strict';
(() => {
  const $=id=>document.getElementById(id), dialog=$('activity-center');
  const node=(tag,cls,text)=>{const n=document.createElement(tag);if(cls)n.className=cls;if(text!==undefined)n.textContent=text;return n;};
  let snapshot=null, catalog=null, loading=false, polling=null, operation=false, listSignature='',branchesLoaded=false;
  MiaoDialogs.bind(dialog);
  const message=value=>{$('activity-center-message').textContent=value;};
  const operationText=value=>typeof value==='string'?value:value?.message||'';
  function render(){
    const locked=loading||operation||Boolean(catalog?.busy||snapshot?.plugins_busy)||(snapshot?.workspaces||[]).some(row=>row.busy||['running','connecting','scanning','paused','stopping'].includes(row.status));
    $('activity-center-refresh').disabled=loading||locked;
    $('activity-source-branch').disabled=locked;
    $('activity-branches-refresh').disabled=locked;
    const ref=catalog?.source_ref||'main',select=$('activity-source-branch');
    if(!Array.from(select.children).some(option=>option.value===ref)){const option=node('option','',ref);option.value=ref;select.append(option);}select.value=ref;
    const revision=catalog?.revision||catalog?.source_commit?.slice(0,10)||'';
    $('activity-source-label').textContent='GitHub 源码仓库 · '+(catalog?.source_ref||'main')+(revision?' · '+revision:'');
    $('activity-folder-path').textContent=catalog?.activities_dir||'正在读取…';
    $('activity-folder-open').disabled=!catalog?.activities_dir||!snapshot;
    if(locked&&!operation&&!loading)message(operationText(catalog?.operation)||operationText(snapshot?.plugins_operation)||'设备正在操作中，请全部停止后再修改活动包。');
    const signature=JSON.stringify([catalog?.catalog,catalog?.installed,locked,loading&&!catalog]);
    if(signature===listSignature)return;listSignature=signature;
    const list=$('activity-center-list');list.replaceChildren();
    const groups=new Map(),visible=new Set();
    function groupFor(id,label){
      if(!groups.has(id)){const group=node('section','activity-source-group'),cards=node('div','activity-center-grid');group.append(node('h3','',label),cards);list.append(group);groups.set(id,cards);}
      return groups.get(id);
    }
    for(const app of catalog?.catalog||[]){const cards=groupFor(app.id,app.label);
      for(const activity of app.activities||[]){
        visible.add(app.id+'/'+activity.id);
        const installed=(catalog.installed||[]).find(item=>item.app===app.id&&item.activity===activity.id);
        const available=activity.revision||activity.source_commit?.slice(0,10)||activity.version;
        const local=activity.installed_revision||activity.installed_source_commit?.slice(0,10)||installed?.source_commit?.slice(0,10)||activity.installed_version;
        const newer=activity.update_available??(activity.installed&&local!==available);
        const card=node('article','activity-download-card');card.append(node('h4','',activity.label),node('p','',`${activity.source==='bundled-recovery'?'离线备份':'仓库代码'} ${available} · ${activity.installed?'已安装 '+local:'尚未安装'}`));
        const actions=node('div','activity-download-actions');const button=node('button','feature-button',activity.source==='bundled-recovery'?'离线恢复':activity.installed?(newer?'更新源码':'重新安装'):'下载活动源码');button.type='button';button.disabled=locked||!activity.compatible;button.addEventListener('click',()=>operate(activity.source==='bundled-recovery'?'restore':'install',app.id,activity.id,activity.source_commit));actions.append(button);
        if(activity.offline_recovery&&activity.source!=='bundled-recovery'){const restore=node('button','feature-button','离线恢复');restore.type='button';restore.disabled=locked;restore.addEventListener('click',()=>operate('restore',app.id,activity.id));actions.append(restore);}
        if(activity.installed){const remove=node('button','text-button','卸载');remove.type='button';remove.disabled=locked;remove.addEventListener('click',()=>operate('uninstall',app.id,activity.id));actions.append(remove);}card.append(actions);if(!activity.compatible)card.append(node('small','activity-incompatible','此活动需要其他版本的框架或运行环境，请先升级框架。'));cards.append(card);
      }
    }
    for(const installed of catalog?.installed||[]){if(visible.has(installed.app+'/'+installed.activity))continue;const row=node('article','activity-download-card');row.append(node('h4','',installed.label||installed.activity),node('p','',`本地已安装 ${installed.version} · 暂未列入当前目录`));const actions=node('div','activity-download-actions'),remove=node('button','text-button','卸载');remove.type='button';remove.disabled=locked;remove.addEventListener('click',()=>operate('uninstall',installed.app,installed.activity));actions.append(remove);row.append(actions);const app=snapshot?.catalog?.find(item=>item.id===installed.app);groupFor(installed.app,app?.label||installed.app).append(row);}
    if(!groups.size)list.append(node('p','activity-center-empty',loading?'正在读取活动目录…':catalog?.error?'目录读取失败；可手动刷新或使用随包离线恢复。':'暂时没有可下载活动，请检查网络或刷新目录。'));
  }
  async function load(refresh=false){
    if(loading)return;loading=true;if(refresh)message('正在更新活动目录…');render();
    try{catalog=await MiaoAPI.request('/api/plugins'+(refresh?'?refresh=1':''),{timeout:65000});message(catalog.error||operationText(catalog.operation)||(catalog.warnings?.length?'部分活动无法列出：'+catalog.warnings.slice(0,3).join('；'):catalog?.transport==='github-archive'?'GitHub API 不可用，已通过官方源码归档读取目录；仅安装所选活动。':catalog?.transport==='pages'?'正在使用静态资源镜像；下载包按固定提交和摘要校验。':'目录已检查；按需下载活动，预装或已下载的活动可离线恢复。'));}
    catch(error){message(error.message);}
    finally{loading=false;render();clearTimeout(polling);if(dialog.open&&(catalog?.busy||operation))polling=setTimeout(()=>load(),700);}
  }
  async function operate(action,app,activity,sourceCommit){
    if(operation||!snapshot)return;operation=true;message(action==='restore'?'正在从本机恢复活动源码，无需联网…':action==='install'?'正在下载所选活动源码并检查兼容性，请稍候…':'正在卸载活动，日记录与恢复包会保留…');render();
    try{await MiaoAPI.request('/api/plugins/'+action,{method:'POST',token:snapshot.token,payload:{app,activity,...(sourceCommit?{source_commit:sourceCommit}:{})},timeout:35000});await load();window.dispatchEvent(new Event('miaotask:plan-changed'));}
    catch(error){message(error.message);}
    finally{operation=false;render();}
  }
  function open(){if(!dialog.open)dialog.showModal();load(true);if(!branchesLoaded)$('activity-branches-refresh').dispatchEvent(new Event('click'));}
  $('activity-center-open').addEventListener('click',open);$('activity-center-close').addEventListener('click',()=>dialog.close());$('activity-center-refresh').addEventListener('click',()=>load(true));
  $('activity-branches-refresh').addEventListener('click',async()=>{
    try{const result=await MiaoAPI.request('/api/plugins/branches',{timeout:35000});const select=$('activity-source-branch');select.replaceChildren();branchesLoaded=true;for(const ref of result.branches){const option=node('option','',ref);option.value=ref;select.append(option);}render();}
    catch(error){message(error.message);}
  });
  $('activity-source-branch').addEventListener('change',async()=>{
    if(!snapshot)return;
    try{await MiaoAPI.request('/api/plugins/source',{method:'POST',token:snapshot.token,payload:{source_ref:$('activity-source-branch').value}});catalog=null;await load(true);message(catalog?.error||'已切换源码分支；已安装活动保持不变，选择下载后才更新。');}
    catch(error){message(error.message);render();}
  });
  $('activity-folder-open').addEventListener('click',async()=>{
    if(!snapshot)return;
    try{await MiaoAPI.request('/api/plugins/open-folder',{method:'POST',token:snapshot.token,payload:{}});}
    catch(error){message(error.message);}
  });
  dialog.addEventListener('close',()=>clearTimeout(polling));window.addEventListener('miaotask:open-activity-center',open);window.addEventListener('miaotask:state',event=>{snapshot=event.detail;if(dialog.open&&Boolean(catalog?.busy)!==Boolean(snapshot.plugins_busy))load();});
})();
