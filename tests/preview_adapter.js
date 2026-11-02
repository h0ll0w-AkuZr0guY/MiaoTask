'use strict';
const vm=require('node:vm'),fs=require('node:fs'),path=require('node:path'),assert=require('node:assert/strict');
(async()=>{
  let requests=0;
  const ctx={URL,Response,console,location:{href:'https://example.invalid/MiaoTask/'},document:{currentScript:{src:'https://example.invalid/MiaoTask/demo/adapter.js'},addEventListener(){}},window:{fetch:async url=>{requests++;assert.equal(String(url),'https://example.invalid/MiaoTask/demo/catalog.json');return new Response(JSON.stringify({version:'demo',catalog:[{id:'taobao',label:'淘宝',package:'demo.example',activities:[{id:'coins',label:'演示活动'}]}]}));}}};
  vm.runInNewContext(fs.readFileSync(path.join(__dirname,'../scripts/site-preview/adapter.js'),'utf8'),ctx);
  const call=async(p,data)=>ctx.window.fetch('/api/'+p+'?device=default',data?{method:'POST',body:JSON.stringify(data)}:{});
  let state=await (await call('state')).json();assert.equal(state.workspaces.length,2);
  assert.equal((await call('run',{})).status,400);
  assert.equal((await call('wifi/pair',{pair_code:'000000'})).status,400);
  await call('task_skip',{task_id:'demo-search'});
  state=await (await call('state')).json();assert.equal(state.tasks[1].enabled,false);
  await call('configure',{config:{max_tasks:2}});assert.equal((await (await call('state')).json()).config.max_tasks,2);
  const created=await (await call('workspaces',{label:'演示新设备'})).json();assert.ok(created.id);
  await assert.rejects(ctx.window.fetch('https://example.org/api/run'));
  assert.equal(requests,1,'Only the generated catalog is fetched; device actions stay in memory');
  console.log('Preview adapter isolation passed');
})().catch(e=>{console.error(e);process.exit(1);});
