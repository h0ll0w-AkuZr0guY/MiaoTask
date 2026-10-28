'use strict';
(() => {
  const $=id=>document.getElementById(id), range=$('preview-size'), image=$('phone-image');
  const dialog=$('preview-dialog'), expanded=$('preview-expanded-image'), open=$('preview-expand');
  const storageKey='miaotask.preview-width.v1';
  const toggle=$('preview-toggle'),sidebar=$('preview-sidebar'),workspace=document.querySelector('.workspace');
  function showSidebar(visible){
    sidebar.hidden=!visible;workspace.classList.toggle('preview-hidden',!visible);
    toggle.setAttribute('aria-pressed',String(visible));
    toggle.classList.toggle('is-open',visible);
  }
  try{showSidebar(localStorage.getItem('miaotask.preview-visible.v1')!=='false');}catch{showSidebar(true);}
  toggle.addEventListener('click',()=>{const visible=sidebar.hidden;showSidebar(visible);try{localStorage.setItem('miaotask.preview-visible.v1',String(visible));}catch{}});
  MiaoDialogs.bind(dialog);
  function resize(value){
    value=Math.round(Math.max(160,Math.min(280,Number(value)||220))/10)*10;
    range.value=String(value);$('preview-size-value').value=value+' px';
    document.querySelector('.preview-card').style.setProperty('--preview-width',value+'px');
    return value;
  }
  try{resize(localStorage.getItem(storageKey));}catch{resize(220);}
  range.addEventListener('input',()=>{const value=resize(range.value);try{localStorage.setItem(storageKey,String(value));}catch{}});
  function ready(){
    if(image.hidden||!image.getAttribute('src')||!image.naturalWidth)return false;
    return new URL(image.currentSrc||image.src,location.origin).searchParams.get('device')===MiaoAPI.device;
  }
  function sync(){
    open.disabled=!ready();
    if(dialog.open&&ready())expanded.src=image.currentSrc||image.src;
  }
  image.addEventListener('load',()=>requestAnimationFrame(sync));
  image.addEventListener('error',()=>{open.disabled=true;});
  open.addEventListener('click',()=>{if(!ready())return;if(!dialog.open)dialog.showModal();sync();});
  $('preview-dialog-close').addEventListener('click',()=>dialog.close());
  dialog.addEventListener('close',()=>expanded.removeAttribute('src'));
  window.addEventListener('miaotask:device-selected',()=>{if(dialog.open)dialog.close();open.disabled=true;});
})();
