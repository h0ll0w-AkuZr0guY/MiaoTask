'use strict';
// Browser-independent checks; no npm packages, phone driver or network access.
const assert=require('node:assert/strict');
const fs=require('node:fs'),path=require('node:path'),vm=require('node:vm');
class Element {
  constructor(tag='div') {this.tagName=tag;this.dataset={};this.handlers={};this.children=[];this.hidden=false;this.open=false;this.disabled=false;this.value='';this.classList={toggle:()=>{}};this.style={setProperty:(key,value)=>{this.style[key]=value;}};}
  addEventListener(name,handler){(this.handlers[name]??=[]).push(handler);}
  fire(name,options={}){for(const handler of this.handlers[name]||[])handler({target:this,...options});}
  dispatchEvent(event){this.fire(event.type,event);}
  removeEventListener(name,handler){this.handlers[name]=(this.handlers[name]||[]).filter(item=>item!==handler);}
  append(...children){this.children.push(...children);}
  replaceChildren(...children){this.children=[...children];}
  get firstChild(){return this.children[0];}
  get lastChild(){return this.children[this.children.length-1];}
  querySelectorAll(){return [];}
  getBoundingClientRect(){return {left:100,top:100,right:400,bottom:500};}
  close(){this.open=false;this.fire('close');}
  showModal(){this.open=true;}
  getAttribute(name){return this[name]||null;}
  setAttribute(name,value){this[name]=value;}
  removeAttribute(name){delete this[name];}
}
function environment(saved='220'){
  const nodes=new Map(),get=id=>{if(!nodes.has(id))nodes.set(id,new Element());return nodes.get(id);};
  const window=new Element(),values=new Map([['miaotask.preview-width.v1',saved]]),card=new Element();
  const context=vm.createContext({window,document:{documentElement:new Element(),getElementById:get,createElement:tag=>new Element(tag),querySelector:()=>card},
    localStorage:{getItem:key=>values.get(key),setItem:(key,value)=>values.set(key,value)},
    MiaoAPI:{device:'alpha'},Event:class{constructor(type){this.type=type;}},CustomEvent:class{constructor(type,options){this.type=type;this.detail=options.detail;}},location:{origin:'http://127.0.0.1:9917'},URL,
    requestAnimationFrame:callback=>callback(),setTimeout:()=>1,clearTimeout:()=>{}});
  const load=name=>vm.runInContext(fs.readFileSync(path.join(__dirname,'../frontend/js/components',name),'utf8'),context);
  load('dialogs.js');context.MiaoDialogs=window.MiaoDialogs;
  return {get,window,values,context,card,load};
}
function checkDialogs(){
  const e=environment(),dialog=e.get('dialog');e.context.MiaoDialogs.bind(dialog);e.context.MiaoDialogs.bind(dialog);
  assert.equal(dialog.handlers.pointerdown.length,1,'binding twice must not add handlers');
  dialog.showModal();dialog.fire('pointerdown',{clientX:150,clientY:150});dialog.fire('click',{clientX:150,clientY:150});
  assert.equal(dialog.open,true,'inside padding must not dismiss');
  dialog.fire('pointerdown',{clientX:150,clientY:150});dialog.fire('click',{clientX:8,clientY:8});
  assert.equal(dialog.open,true,'dragging from inside must not dismiss');
  dialog.fire('pointerdown',{clientX:8,clientY:8});dialog.fire('click',{clientX:8,clientY:8});
  assert.equal(dialog.open,false,'a backdrop click dismisses');
}
function checkPreview(){
  const e=environment('invalid'),image=e.get('phone-image'),dialog=e.get('preview-dialog'),button=e.get('preview-expand');
  image.hidden=true;button.disabled=true;e.load('preview-controls.js');
  assert.equal(e.get('preview-sidebar').hidden,false);
  e.get('preview-toggle').fire('click');
  assert.equal(e.get('preview-sidebar').hidden,true);
  assert.equal(e.values.get('miaotask.preview-visible.v1'),'false');
  e.get('preview-toggle').fire('click');
  assert.equal(e.get('preview-sidebar').hidden,false);
  assert.equal(e.get('preview-size').value,'220');
  e.get('preview-size').value='999';e.get('preview-size').fire('input');
  assert.equal(e.get('preview-size').value,'280');assert.equal(e.values.get('miaotask.preview-width.v1'),'280');
  image.hidden=false;image.naturalWidth=720;image.src=image.currentSrc='http://127.0.0.1:9917/api/screenshot?device=alpha';image.fire('load');
  assert.equal(button.disabled,false);button.fire('click');assert.equal(dialog.open,true);
  assert.match(e.get('preview-expanded-image').src,/device=alpha/);
  e.context.MiaoAPI.device='beta';e.window.fire('miaotask:device-selected');
  assert.equal(dialog.open,false);assert.equal(button.disabled,true);assert.equal(e.get('preview-expanded-image').getAttribute('src'),null);
  image.fire('load');assert.equal(button.disabled,true,'late old-device image cannot enable preview');
  image.src=image.currentSrc='http://127.0.0.1:9917/api/screenshot?device=beta';image.fire('load');
  assert.equal(button.disabled,false);button.fire('click');assert.match(e.get('preview-expanded-image').src,/device=beta/);
  const blocked=environment();blocked.context.localStorage.getItem=()=>{throw Error('blocked');};blocked.context.localStorage.setItem=()=>{throw Error('blocked');};
  blocked.load('preview-controls.js');blocked.get('preview-size').fire('input');assert.equal(blocked.get('preview-size').value,'220');
}
async function checkActivityGrouping(){
  const e=environment(),requests=[];
  const installed=Array.from({length:5},(_,i)=>({app:'sample',activity:'activity-'+i,label:'Local '+i,version:'0.0.1'}));
  const catalog={catalog:[{id:'sample',label:'Sample',activities:[{id:'activity-0',label:'Remote',installed:true,compatible:true,version:'0.0.1'}]}],installed};
  e.context.MiaoAPI.request=async url=>{requests.push(url);return catalog;};
  e.load('activity-center.js');e.window.fire('miaotask:state',{detail:{token:'synthetic',catalog:[{id:'sample',label:'Sample'}],workspaces:[]}});
  e.get('activity-center-open').fire('click');await new Promise(resolve=>setImmediate(resolve));
  const groups=e.get('activity-center-list').children;
  assert.equal(groups.length,1);assert.equal(groups[0].children[1].children.length,5,'unlisted local activities use the same grid');
  assert.equal(requests[0],'/api/plugins','opening reuses the catalog instead of forcing a remote refresh');
  e.get('activity-center-refresh').fire('click');await new Promise(resolve=>setImmediate(resolve));
  assert.equal(requests[1],'/api/plugins?refresh=1');
}
async function checkRankings(){
  const e=environment(),dialog=e.get('rankings-dialog'),requests=[];
  const activity=new Element();activity.dataset={rankingGroup:'activity'};
  dialog.querySelectorAll=selector=>selector==='[data-ranking-group]'?[activity]:[];
  e.context.MiaoAPI.request=url=>new Promise((resolve,reject)=>requests.push({url,resolve,reject}));
  e.load('rankings.js');e.get('rankings-open').fire('click');activity.fire('click');
  assert.match(requests[1].url,/group=activity/);
  const data=label=>({rows:[{rank:1,device_key:'1234567890abcdef',days:2,app_label:label,activity_label:'Daily',stats:{elapsed:60,completed:2,skipped:1,failed:0}}],updated_at:'2026-01-01T12:00:00Z',storage_dir:'synthetic'});
  requests[1].resolve(data('New scope'));await new Promise(resolve=>setImmediate(resolve));
  assert.equal(e.get('rankings-rows').children[0].children[2].textContent,'New scope');
  requests[0].resolve(data('Stale scope'));await new Promise(resolve=>setImmediate(resolve));
  assert.equal(e.get('rankings-rows').children[0].children[2].textContent,'New scope');
  e.get('rankings-refresh').fire('click');e.get('rankings-close').fire('click');
  requests[2].resolve(data('Late after close'));await new Promise(resolve=>setImmediate(resolve));
  assert.equal(e.get('rankings-rows').children.length,0,'closed dialog must not accept a late response');
}
function checkThemes(){
  const e=environment(),dialog=e.get('themes-dialog'),blue=new Element(),night=new Element();
  blue.dataset.themeChoice='blue';night.dataset.themeChoice='night';dialog.querySelectorAll=()=>[blue,night];
  e.load('themes.js');e.window.fire('DOMContentLoaded');blue.fire('click');
  assert.equal(e.context.document.documentElement.dataset.theme,'blue');
  assert.equal(e.values.get('miaotask.theme.v1'),'blue');
  night.fire('click');assert.equal(e.context.document.documentElement.dataset.theme,'night');
  e.window.fire('storage',{key:'miaotask.theme.v1',newValue:'arbitrary'});
  assert.equal(e.context.document.documentElement.dataset.theme,'warm','unknown themes cannot enter CSS selectors');
  const blocked=environment();blocked.context.localStorage.getItem=()=>{throw Error('blocked');};blocked.context.localStorage.setItem=()=>{throw Error('blocked');};
  blocked.load('themes.js');assert.equal(blocked.context.document.documentElement.dataset.theme,'warm');
}
async function checkDevicePreparation(){
  const e=environment(),api=e.context.MiaoAPI,requests=[];
  api.selectDevice=id=>{api.device=id;};
  api.request=(url,options)=>{if(url==='/api/inventory')return Promise.resolve({devices:[]});return new Promise(resolve=>requests.push({url,options,resolve}));};
  const row=id=>({id,label:id,status:'ready',connected:true,stats:{completed:0}});
  e.load('device-manager.js');e.window.fire('miaotask:state',{detail:{workspace_id:'alpha',token:'synthetic',workspaces:[row('alpha')],device:{serial:'synthetic-original'}}});
  e.get('add-device').fire('click');e.get('new-device-name').value='New';
  const pending=e.window.MiaoDevices.prepare();assert.equal(requests[0].url,'/api/workspaces');
  requests[0].resolve({id:'beta',devices:[row('alpha'),row('beta')]});await new Promise(resolve=>setImmediate(resolve));
  assert.equal(api.device,'beta');
  e.window.fire('miaotask:state',{detail:{workspace_id:'beta',token:'synthetic',workspaces:[row('alpha'),row('beta')]}});
  assert.equal(await pending,'beta','connection must wait for the new workspace snapshot');
  const renamed=e.window.MiaoDevices.prepare();assert.equal(requests[1].url,'/api/workspaces/rename');
  assert.equal(requests[1].options.payload.id,'beta');requests[1].resolve({});await renamed;
  api.device='alpha';await assert.rejects(e.window.MiaoDevices.prepare(),/设备已切换/);
  assert.equal(requests.length,2,'stale editors must not mutate another workspace');
}
(async()=>{checkDialogs();checkPreview();checkThemes();await checkActivityGrouping();await checkRankings();await checkDevicePreparation();console.log('PASS dialogs, preview, themes, catalog, rankings, device preparation/isolation');})().catch(error=>{console.error(error);process.exitCode=1;});
