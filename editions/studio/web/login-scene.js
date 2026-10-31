/* Visual state only. Authentication decides when enterWorkspace may be called. */
(()=>{
  'use strict';
  if(!document.body.classList.contains('login-deep-sea'))return;
  const stage=document.querySelector('#stage'),panel=document.querySelector('#loginPanel'),cover=document.querySelector('.cover-content'),video=document.querySelector('#sceneVideo'),curtain=document.querySelector('#curtain');
  if(!stage||!panel||!cover)return;
  const reduced=matchMedia('(prefers-reduced-motion:reduce)'),fine=matchMedia('(hover:hover) and (pointer:fine)'),compact=matchMedia('(max-width:700px)');
  const enter=document.querySelector('#enterBtn'),submit=document.querySelector('#submitBtn'),status=document.querySelector('#sceneStatus');
  const controls=[document.querySelector('#backBtn')].filter(Boolean);
  let phase='cover',busy=false,revealing=false,epoch=0,entryHandler=null,entryPending=false,navigating=false,navigationPromise=null,navigateNow=null;
  const animations=new Set(),ease='cubic-bezier(.16,1,.3,1)';

  function stopAnimations(){epoch++;for(const a of animations)a.cancel();animations.clear();}
  function animate(el,frames,duration=420,delay=0){if(!el||reduced.matches||document.hidden)return Promise.resolve();try{const a=el.animate(frames,{duration,delay,easing:ease,fill:'backwards'});animations.add(a);return a.finished.catch(()=>{}).finally(()=>animations.delete(a));}catch(_){return Promise.resolve();}}
  function syncMedia(){const stop=reduced.matches||compact.matches||!fine.matches||document.hidden||phase==='entering';if(video){if(stop)video.pause();else video.play().catch(()=>{});}}
  function setPhase(next){phase=next;stage.dataset.state=next;const showPanel=next==='form'||next==='auth',showCover=['cover','cover-busy','revealing'].includes(next);panel.inert=!showPanel;panel.setAttribute('aria-hidden',String(!showPanel));cover.inert=!showCover;cover.setAttribute('aria-hidden',String(!showCover));syncMedia();stage.dispatchEvent(new CustomEvent('login:phase',{detail:{phase:next}}));}
  function updateControls(){if(submit)submit.disabled=busy||revealing||navigating;if(enter)enter.disabled=busy||revealing||navigating;for(const el of controls)el.disabled=busy||revealing||navigating;panel.setAttribute('aria-busy',String(busy));stage.dataset.busy=String(busy);}
  function setBusy(value){if(navigating)return;busy=Boolean(value);if(busy)setPhase(phase==='cover'||phase==='cover-busy'?'cover-busy':'auth');else if(phase==='cover-busy')setPhase('cover');else if(phase==='auth')setPhase('form');updateControls();}
  async function showForm(){if(navigating||busy||revealing)return;stopAnimations();const token=epoch,fromCover=phase==='cover'||phase==='cover-busy',focusInput=document.activeElement===enter;revealing=true;updateControls();
    if(fromCover){setPhase('revealing');await animate(cover,[{opacity:1,transform:'none'},{opacity:0,transform:'translateX(-24px) scale(.985)'}],180);if(token!==epoch)return;}
    setPhase('form');
    const reveal=animate(panel,[{opacity:0,transform:'perspective(1000px) translate(32px,14px) rotateY(-7deg) scale(.96)',clipPath:'inset(0 0 35% 12% round 16px)'},{opacity:1,transform:'perspective(1000px) translate(0,0) rotateY(0) scale(1)',clipPath:'inset(0 0 0 0 round 16px)'}],600);
    panel.querySelectorAll('#title,#subtitle,#tabs,.field,#submitBtn').forEach((el,i)=>animate(el,[{opacity:0,transform:'translateX(14px)'},{opacity:1,transform:'none'}],320,Math.min(i*35,140)));
    await reveal;if(token!==epoch)return;revealing=false;updateControls();if(!busy&&!navigating&&fine.matches&&focusInput)document.querySelector('#email')?.focus({preventScroll:true});
  }
  function error(){if(navigating)return;stopAnimations();busy=false;revealing=false;setPhase('form');updateControls();const err=document.querySelector('#err');if(err&&err.textContent.trim()){err.tabIndex=-1;err.focus({preventScroll:true});err.scrollIntoView({block:'nearest',behavior:'instant'});}}
  function replay(){if(busy||revealing||navigating)return;stopAnimations();setPhase('cover');updateControls();if(status)status.textContent='';
    animate(cover,[{opacity:0,transform:'translateY(14px)'},{opacity:1,transform:'none'}],650,90);
    animate(stage.querySelector('.stage-header'),[{opacity:0},{opacity:1}],500);
  }
  function setEntryLabel(text){if(!enter)return;const label=enter.querySelector('.entry-label');if(label)label.textContent=String(text);else enter.textContent=String(text);}
  function onEnter(handler){entryHandler=typeof handler==='function'?handler:null;return()=>{if(entryHandler===handler)entryHandler=null;};}
  async function requestEntry(){if(entryPending||busy||navigating)return;entryPending=true;try{await(entryHandler?entryHandler():showForm());}catch(_){error();}finally{entryPending=false;}}
  function enterWorkspace(url){
    if(navigationPromise)return navigationPromise;
    const destination=new URL(url,location.href);if(destination.origin!==location.origin||!/^\/(admin|member)(?:[/?#]|$)/.test(destination.pathname+destination.search+destination.hash))throw new Error('Invalid workspace destination');
    navigating=true;busy=true;stopAnimations();setPhase('entering');updateControls();if(status)status.textContent='登录成功，正在进入工作台…';
    navigationPromise=new Promise(resolve=>{let done=false,timer=0;const finish=()=>{if(done)return;done=true;clearTimeout(timer);navigateNow=null;try{location.assign(destination.href);}catch(_){try{location.href=destination.href;}catch(__){if(status)status.textContent='无法打开工作台，请刷新页面后重试。';}}resolve();};navigateNow=finish;timer=setTimeout(finish,850);
      if(reduced.matches||document.hidden||!curtain){finish();return;}
      try{curtain.style.visibility='visible';animate(curtain,[{clipPath:'inset(100% 0 0 0)',opacity:.6},{clipPath:'inset(0 0 0 0)',opacity:1}],430).then(finish,finish);}catch(_){finish();}
    });return navigationPromise;
  }
  window.LoginScene={showForm,setBusy,error,enterWorkspace,setEntryLabel,onEnter};
  enter?.addEventListener('click',requestEntry);document.querySelector('#backBtn')?.addEventListener('click',replay);
  stage.querySelector('.brand')?.addEventListener('click',event=>{if(event.currentTarget.tagName==='A')event.preventDefault();replay();});
  function settleReveal(){if(!revealing)return;revealing=false;setPhase('form');updateControls();}
  const resetButtons=[];
  stage.querySelectorAll('.primary').forEach(button=>{
    function reset(){button.dataset.pressed='false';button.style.removeProperty('--hover-x');button.style.removeProperty('--hover-y');button.style.removeProperty('--light-x');button.style.removeProperty('--light-y');}
    resetButtons.push(reset);
    button.addEventListener('pointermove',event=>{if(!fine.matches||reduced.matches||button.disabled)return;const rect=button.getBoundingClientRect(),x=Math.max(0,Math.min(1,(event.clientX-rect.left)/rect.width)),y=Math.max(0,Math.min(1,(event.clientY-rect.top)/rect.height));button.style.setProperty('--hover-x',`${((x-.5)*8).toFixed(2)}px`);button.style.setProperty('--hover-y',`${((y-.5)*6).toFixed(2)}px`);button.style.setProperty('--light-x',`${(x*100).toFixed(1)}%`);button.style.setProperty('--light-y',`${(y*100).toFixed(1)}%`);},{passive:true});
    button.addEventListener('pointerdown',()=>{if(!button.disabled)button.dataset.pressed='true';});
    button.addEventListener('pointerleave',reset);button.addEventListener('pointercancel',reset);button.addEventListener('blur',reset);
    button.addEventListener('keydown',event=>{if(['Enter',' '].includes(event.key)&&!button.disabled)button.dataset.pressed='true';});button.addEventListener('keyup',reset);
  });
  window.addEventListener('pointerup',()=>resetButtons.forEach(reset=>reset()));window.addEventListener('blur',()=>resetButtons.forEach(reset=>reset()));
  document.addEventListener('visibilitychange',()=>{if(document.hidden){stopAnimations();settleReveal();resetButtons.forEach(reset=>reset());if(navigateNow)navigateNow();}syncMedia();});
  reduced.addEventListener('change',()=>{if(reduced.matches){stopAnimations();settleReveal();resetButtons.forEach(reset=>reset());if(navigateNow)navigateNow();}syncMedia();});fine.addEventListener('change',syncMedia);compact.addEventListener('change',syncMedia);
  window.addEventListener('pagehide',()=>{stopAnimations();settleReveal();resetButtons.forEach(reset=>reset());video?.pause();});window.addEventListener('pageshow',syncMedia);
  replay();
})();
