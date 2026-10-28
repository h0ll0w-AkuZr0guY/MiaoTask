'use strict';
(() => {
  const key='miaotask.theme.v1',allowed=new Set(['warm','blue','night']);
  let current='warm';
  function apply(theme){current=allowed.has(theme)?theme:'warm';document.documentElement.dataset.theme=current;try{localStorage.setItem(key,current);}catch{}}
  try{apply(localStorage.getItem(key));}catch{apply('warm');}
  window.addEventListener('DOMContentLoaded',()=>{
    const dialog=document.getElementById('themes-dialog');MiaoDialogs.bind(dialog);
    const render=()=>{for(const button of dialog.querySelectorAll('[data-theme-choice]'))button.setAttribute('aria-pressed',String(button.dataset.themeChoice===current));};
    document.getElementById('themes-open').addEventListener('click',()=>{render();if(!dialog.open)dialog.showModal();});
    document.getElementById('themes-close').addEventListener('click',()=>dialog.close());
    for(const button of dialog.querySelectorAll('[data-theme-choice]'))button.addEventListener('click',()=>{apply(button.dataset.themeChoice);render();});
    // Keep another open MiaoTask tab in the same browser visually consistent.
    window.addEventListener('storage',event=>{if(event.key===key){current=allowed.has(event.newValue)?event.newValue:'warm';document.documentElement.dataset.theme=current;render();}});
    render();
  });
})();
