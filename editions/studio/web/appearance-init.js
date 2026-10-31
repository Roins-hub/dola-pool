/* Appearance only: this file never reads authentication or makes a request. */
(()=>{
  'use strict';
  const legacyThemeKey='dola-pool.ui.theme',motionKey='dola-pool.ui.motion',root=document.documentElement;
  const defaults={visualV4Night:true,loginArt:true,hoverGlow:true};
  const features=Object.assign({},defaults,window.DOLA_VISUAL_CONFIG||{});
  const reduced=matchMedia('(prefers-reduced-motion: reduce)');
  const read=(name,fallback)=>{try{return localStorage.getItem(name)||fallback;}catch{return fallback;}};
  const write=(name,value)=>{try{localStorage.setItem(name,value);}catch{}};
  let motion=read(motionKey,'on');
  try{localStorage.removeItem(legacyThemeKey);}catch{}
  function apply(){
    root.dataset.theme='dark';root.style.colorScheme='dark';
    root.dataset.visualV4Night=features.visualV4Night?'on':'off';
    root.dataset.loginArt=features.loginArt?'on':'off';root.dataset.hoverGlow=features.hoverGlow?'on':'off';
    root.dataset.motion=motion==='on'&&!reduced.matches?'on':'off';
    document.dispatchEvent(new CustomEvent('dola-appearance-change'));
  }
  function setTheme(value){if(value==='dark')apply();}
  function setMotion(enabled){motion=enabled?'on':'off';write(motionKey,motion);apply();}
  window.DolaAppearance={features,setTheme,setMotion,get preference(){return 'dark';},get motion(){return motion;},refresh:apply};
  reduced.addEventListener('change',apply);
  window.addEventListener('storage',event=>{if(event.key===legacyThemeKey){try{localStorage.removeItem(legacyThemeKey);}catch{}apply();}else if(event.key===motionKey){motion=event.newValue==='off'?'off':'on';apply();}});
  apply();
})();
