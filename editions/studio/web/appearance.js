/* Optional visual controls and login art. No business handlers or requests. */
(()=>{
  'use strict';
  const appearance=window.DolaAppearance;if(!appearance)return;
  function boot(){
    const login=document.body.classList.contains('login-app');
    const controls=document.createElement('div');controls.className='appearance-controls';
    const label=document.createElement('span');label.className='appearance-label';label.textContent='外观';
    const theme=document.createElement('span');theme.className='appearance-theme';theme.setAttribute('aria-label','当前主题：夜间');theme.textContent='夜间';
    controls.append(label,theme);
    const motionButton=document.createElement('button');motionButton.type='button';motionButton.className='appearance-motion'+(login?'':' appearance-motion-compact');motionButton.addEventListener('click',()=>appearance.setMotion(appearance.motion!=='on'));controls.append(motionButton);
    if(login)(document.querySelector('.login-shell')||document.body).append(controls);
    else (document.querySelector('.head-r')||document.body).prepend(controls);
    let menuMotion;
    const footer=document.querySelector('.sidebar-footer');
    if(!login&&footer){menuMotion=document.createElement('button');menuMotion.type='button';menuMotion.className='appearance-menu-motion btn';menuMotion.addEventListener('click',()=>appearance.setMotion(appearance.motion!=='on'));footer.append(menuMotion);}
    function sync(){const paused=appearance.motion!=='on',label=paused?'开启动效':'暂停动效';motionButton.setAttribute('aria-label',label);motionButton.title=label;motionButton.setAttribute('aria-pressed',String(paused));if(login)motionButton.textContent=label;else motionButton.innerHTML='<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">'+(paused?'<path d="m8 5 11 7-11 7Z"/>':'<path d="M8 5v14M16 5v14"/>')+'</svg>';if(menuMotion){menuMotion.textContent=label;menuMotion.setAttribute('aria-pressed',String(paused));}}
    document.addEventListener('dola-appearance-change',sync);sync();
    if(!login)return;
    const art=document.querySelector('.login-aside'),layer=document.querySelector('.login-art');
    if(!art||!layer)return;
    let visible=true,frame=0;
    function syncArt(){frame=0;const focused=document.activeElement?.matches('input,textarea,select');art.dataset.artPaused=visible&&!document.hidden&&!focused?'false':'true';}
    function afterFocus(){if(frame)cancelAnimationFrame(frame);frame=requestAnimationFrame(syncArt);}
    const observer=new IntersectionObserver(entries=>{visible=entries[0].isIntersecting;syncArt();});observer.observe(art);
    document.addEventListener('focusin',syncArt);document.addEventListener('focusout',afterFocus);
    document.addEventListener('visibilitychange',syncArt);document.addEventListener('dola-appearance-change',syncArt);
    window.addEventListener('pagehide',()=>{if(frame)cancelAnimationFrame(frame);art.dataset.artPaused='true';observer.disconnect();});
    window.addEventListener('pageshow',()=>{observer.observe(art);syncArt();});
  }
  if(document.readyState==='loading')document.addEventListener('DOMContentLoaded',boot,{once:true});else boot();
})();
