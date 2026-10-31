/* Visual entrance and light lifecycle only; does not touch form submission. */
(()=>{
  'use strict';
  const body=document.querySelector('.studio-login');if(!body)return;
  const reduced=matchMedia('(prefers-reduced-motion:reduce)'),root=document.documentElement;
  let animations=[];
  function active(){return !document.hidden&&!reduced.matches&&root.dataset.motion==='on'&&!document.activeElement?.matches('input,textarea,select');}
  function sync(){body.dataset.sceneActive=String(active());if(!active()){for(const animation of animations)animation.cancel();animations=[];}}
  function enter(){
    sync();if(!active())return;
    const reveal=(selector,frames,delay,duration)=>{const node=document.querySelector(selector);if(node)animations.push(node.animate(frames,{duration,delay,easing:'cubic-bezier(.16,1,.3,1)'}));};
    reveal('.studio-header',[{opacity:.5,transform:'translateY(-5px)'},{opacity:1,transform:'none'}],0,520);
    reveal('.login-intro',[{opacity:.45,transform:'translateY(12px)',filter:'blur(3px)'},{opacity:1,transform:'none',filter:'blur(0)'}],70,780);
    reveal('.login-content .panel',[{opacity:.5,transform:'translateY(10px)'},{opacity:1,transform:'none'}],160,670);
    reveal('.login-art',[{clipPath:'inset(0 6% 0 0)',opacity:.4},{clipPath:'inset(0 0 0 0)',opacity:1}],0,1100);
  }
  document.addEventListener('dola-appearance-change',sync);document.addEventListener('visibilitychange',sync);
  document.addEventListener('focusin',sync);document.addEventListener('focusout',()=>requestAnimationFrame(sync));
  reduced.addEventListener('change',sync);window.addEventListener('pagehide',()=>{body.dataset.sceneActive='false';for(const animation of animations)animation.cancel();});window.addEventListener('pageshow',sync);
  if(document.readyState==='loading')document.addEventListener('DOMContentLoaded',enter,{once:true});else enter();
})();
