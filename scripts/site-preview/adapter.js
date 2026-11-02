'use strict';
// This adapter is copied only to Pages. It is never loaded by the desktop app.
(() => {
  const nativeFetch=window.fetch.bind(window), base=new URL('.',document.currentScript.src);
  const clone=value=>JSON.parse(JSON.stringify(value));
  let catalog=[],version='',installed=new Set(),nextId=3;
  const tasks=()=>[
    {task_id:'demo-browse',title:'浏览精选好物',detail:'浏览15秒，返回后核验进度 · 演示',kind:'browse',current:0,total:1,status:'pending',enabled:true,controllable:true},
    {task_id:'demo-search',title:'搜索并浏览',detail:'可实时选择执行或跳过 · 演示',kind:'search',current:0,total:1,status:'pending',enabled:true,controllable:true},
    {task_id:'demo-done',title:'领取任务奖励',kind:'claim',current:1,total:1,status:'done',enabled:false,controllable:false}
  ];
  const workspaces=new Map();
  function create(id,label){const value={workspace_id:id,workspace_label:label,connected:true,status:'ready',busy:false,app:'taobao',activity:'coins',activity_plan:[{app:'taobao',activity:'coins'}],tasks:tasks(),config:{},stats:{},all_instances:false};workspaces.set(id,value);return value;}
  create('default','演示手机');create('demo-tablet','演示平板');
  const ready=nativeFetch(new URL('catalog.json',base)).then(r=>{if(!r.ok)throw Error('演示目录未能加载');return r.json();}).then(data=>{catalog=data.catalog;version=data.version;installed=new Set(catalog.flatMap(a=>a.activities.map(t=>a.id+'/'+t.id)));});
  const rows=()=>[...workspaces.values()].map(w=>({id:w.workspace_id,label:w.workspace_label,connected:true,status:'ready',busy:false,stats:{completed:0},message:'浏览器演示设备，无实际连接'}));
  function snapshot(w){const app=catalog.find(a=>a.id===w.app)||catalog[0];const instance=app.id+':demo';return {...clone(w),catalog:catalog.map(a=>({...a,activities:a.activities.filter(t=>installed.has(a.id+'/'+t.id))})),version,token:'demo-only',workspaces:rows(),app_instances:[{id:instance,label:app.label+' · 演示实例',package:app.package,user_id:0}],selected_instance:instance,package:app.package,task_scope:'demo:'+w.workspace_id,device:{serial:'demo-device',type:'uiautomator2'},message:'在线演示：可体验活动选择、设备管理、设置和任务队列；真实执行请下载桌面版。',phase:'ready',source:'demo',image_sequence:1,image_updated_at:new Date().toISOString(),logs:[{id:1,time:new Date().toISOString(),level:'INFO',message:'演示数据已加载，未连接任何真实设备。'}]};}
  function plugins(){return {catalog:catalog.map(a=>({...a,activities:a.activities.map(t=>({...t,app:a.id,installed:installed.has(a.id+'/'+t.id),version:'demo',revision:'demo',installed_revision:'demo',compatible:true,source:'pages'}))})),installed:[],source_ref:'main',revision:'demo',transport:'pages',activities_dir:'桌面版 exe 旁的 activities/（此处仅演示）',warnings:[]};}
  const blocked='在线预览不会连接手机、下载代码或执行任务。请下载桌面版进行真实操作。';
  window.fetch=async(input,options={})=>{
    const url=new URL(typeof input==='string'||input instanceof URL?input:input.url,location.href);
    if(url.origin!==new URL(location.href).origin)throw Error('演示页面不访问外部服务');
    if(!url.pathname.startsWith('/api/')){
      return nativeFetch(input,options);
    }
    try{
      await ready;
      const path=url.pathname,payload=options.body?JSON.parse(options.body):{},id=url.searchParams.get('device')||'default',w=workspaces.get(id);
      let result;
      if(path==='/api/workspaces'&&options.method==='POST'){if(workspaces.size>=8)throw Error('最多演示8台设备');const id='demo-'+nextId++;create(id,String(payload.label||'演示设备 '+nextId).slice(0,40));result={id,devices:rows()};}
      else if(path==='/api/workspaces')result={devices:rows()};
      else if(path==='/api/workspaces/rename'){const target=workspaces.get(payload.id);if(!target)throw Error('设备不存在');target.workspace_label=String(payload.label).slice(0,40);result={devices:rows()};}
      else if(path==='/api/workspaces/remove'){workspaces.delete(payload.id);result={devices:rows()};}
      else if(path==='/api/rankings'){result={rows:[],updated_at:new Date().toISOString(),storage_dir:'桌面版使用系统用户目录（此处没有真实统计）'};}
      else if(path==='/api/plugins')result=plugins();
      else if(path==='/api/plugins/branches')result={branches:['main']};
      else if(/^\/api\/plugins\/(install|restore|uninstall)$/.test(path)){const key=payload.app+'/'+payload.activity;if(path.endsWith('uninstall'))installed.delete(key);else installed.add(key);result={ok:true};}
      else if(path==='/api/inventory'||path==='/api/emulators')result={devices:[{serial:'demo-device',model:'演示设备（非真实连接）',type:'uiautomator2',status:'device'}],errors:[],candidates:[]};
      else if(path==='/api/wifi/devices')result={services:[],devices:[]};
      else if(!w)throw Error('演示工作区不存在');
      else if(path==='/api/state')result=snapshot(w);
      else if(path==='/api/select_pipeline'){w.activity_plan=payload.activities;const first=payload.activities[0];if(first){w.app=first.app;w.activity=first.activity;}result={ok:true};}
      else if(path==='/api/reset_tasks'){w.tasks=tasks();result={ok:true};}
      else if(path==='/api/configure'){w.config=clone(payload.config||{});result={ok:true};}
      else if(path==='/api/task_skip'||path==='/api/task_execute'){const task=w.tasks.find(t=>t.task_id===payload.task_id);if(task?.controllable){task.enabled=path.endsWith('task_execute');task.status=task.enabled?'pending':'disabled';}result={ok:true};}
      else throw Error(blocked);
      return new Response(JSON.stringify(result),{status:200,headers:{'Content-Type':'application/json'}});
    }catch(e){return new Response(JSON.stringify({error:e.message}),{status:400,headers:{'Content-Type':'application/json'}});}
  };
  document.addEventListener('DOMContentLoaded',()=>{
    // Direct export anchors cannot invoke the fetch adapter; keep them local too.
    document.addEventListener('click',e=>{const link=e.target.closest('a');if(link&&new URL(link.href).pathname.startsWith('/api/')){e.preventDefault();window.dispatchEvent(new CustomEvent('miaotask:error',{detail:'在线预览没有真实设备记录可导出。'}));}});
  });
})();
