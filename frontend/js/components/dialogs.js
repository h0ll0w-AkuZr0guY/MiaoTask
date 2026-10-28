'use strict';
(() => {
  const bound=new WeakSet();
  function bind(dialog){
    if(bound.has(dialog))return;
    bound.add(dialog);
    let outsideStart=false;
    const outside=event=>{const box=dialog.getBoundingClientRect();return event.clientX<box.left||event.clientX>box.right||event.clientY<box.top||event.clientY>box.bottom;};
    dialog.addEventListener('pointerdown',event=>{outsideStart=event.target===dialog&&outside(event);});
    dialog.addEventListener('click',event=>{if(outsideStart&&event.target===dialog&&outside(event))dialog.close();outsideStart=false;});
    dialog.addEventListener('close',()=>{outsideStart=false;});
  }
  window.MiaoDialogs=Object.freeze({bind});
})();
