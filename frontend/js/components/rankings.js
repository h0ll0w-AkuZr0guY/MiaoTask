'use strict';
(() => {
  const $=id=>document.getElementById(id), dialog=$('rankings-dialog');
  const node=(tag,cls,text)=>{const n=document.createElement(tag);if(cls)n.className=cls;if(text!==undefined)n.textContent=text;return n;};
  let group='app',period='all',metric='elapsed',request=0;
  MiaoDialogs.bind(dialog);
  function duration(seconds){const hours=Math.floor(seconds/3600),minutes=Math.floor(seconds%3600/60);return hours?`${hours}时 ${minutes}分`:`${minutes}分 ${Math.floor(seconds%60)}秒`;}
  async function load(){
    const current=++request;
    $('rankings-refresh').disabled=true;$('rankings-status').textContent='正在读取本机累计记录…';
    // Clear the previous scope while a new grouping is loading.
    $('rankings-rows').replaceChildren();
    for(const button of dialog.querySelectorAll('[data-ranking-group]'))button.setAttribute('aria-pressed',String(button.dataset.rankingGroup===group));
    for(const button of dialog.querySelectorAll('[data-ranking-period]'))button.setAttribute('aria-pressed',String(button.dataset.rankingPeriod===period));
    for(const button of dialog.querySelectorAll('[data-ranking-metric]')){const selected=button.dataset.rankingMetric===metric;button.setAttribute('aria-pressed',String(selected));button.closest('th').setAttribute('aria-sort',selected?'descending':'none');}
    $('rankings-activity-heading').hidden=group!=='activity';
    try{
      const data=await MiaoAPI.request(`/api/rankings?group=${group}&period=${period}&metric=${metric}`,{timeout:20000});
      if(current!==request||!dialog.open)return;
      const body=$('rankings-rows');
      for(const item of data.rows){
        const row=node('tr'),device=node('td','ranking-device');
        device.title='设备代号：'+item.device_key;
        device.append(node('strong','',item.device_label||'设备 · '+item.device_key.slice(0,8)),node('small','',(item.device_label?item.device_key.slice(0,8)+' · ':'')+item.days+' 天记录'));
        row.append(node('td','ranking-place',String(item.rank)),device,node('td','',item.app_label));
        const activity=node('td','',item.activity_label);activity.hidden=group!=='activity';row.append(activity);
        row.append(node('td','ranking-number',duration(item.stats.elapsed)));
        for(const key of ['completed','skipped','failed'])row.append(node('td','ranking-number',String(item.stats[key])));
        body.append(row);
      }
      if(!data.rows.length){const cell=node('td','ranking-empty','还没有累计记录，执行任务后会显示在这里。');cell.colSpan=group==='activity'?8:7;const row=node('tr');row.append(cell);body.append(row);}
      $('rankings-status').textContent=`${data.rows.length} 组 · ${period==='today'?'今日':'全部日期'} · 更新于 ${new Date(data.updated_at).toLocaleTimeString('zh-CN',{hour12:false})}`+(data.ignored?' · 部分无效或超限记录未计入':'');
      $('rankings-folder').textContent=data.storage_dir;
    }catch(error){if(current===request&&dialog.open)$('rankings-status').textContent=error.message;}
    finally{if(current===request)$('rankings-refresh').disabled=false;}
  }
  $('rankings-open').addEventListener('click',()=>{if(!dialog.open)dialog.showModal();load();});
  $('rankings-close').addEventListener('click',()=>dialog.close());
  $('rankings-refresh').addEventListener('click',load);
  dialog.addEventListener('close',()=>{request++;$('rankings-refresh').disabled=false;});
  for(const button of dialog.querySelectorAll('[data-ranking-group]'))button.addEventListener('click',()=>{group=button.dataset.rankingGroup;load();});
  for(const button of dialog.querySelectorAll('[data-ranking-period]'))button.addEventListener('click',()=>{period=button.dataset.rankingPeriod;load();});
  for(const button of dialog.querySelectorAll('[data-ranking-metric]'))button.addEventListener('click',()=>{metric=button.dataset.rankingMetric;load();});
})();
