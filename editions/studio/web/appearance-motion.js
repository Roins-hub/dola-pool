/* Optional visual feedback. No authentication, generation, upload or polling logic. */
(()=>{
  'use strict';
  const root=document.documentElement;
  const enabled=()=>root.dataset.motion==='on'&&!document.hidden;
  const animations=new Map();
  function play(node,frames,options){
    if(!node||!enabled()||!node.getClientRects().length)return;
    animations.get(node)?.cancel();
    const animation=node.animate(frames,{duration:240,easing:'cubic-bezier(.16,1,.3,1)',...options});
    animations.set(node,animation);
    animation.finished.catch(()=>{}).finally(()=>{if(animations.get(node)===animation)animations.delete(node);});
  }
  const nav=document.querySelector('.sidebar nav');
  let rail,navFrame=0,previous;
  function syncNav(){
    navFrame=0;
    const active=nav?.querySelector('button.on,a.on');
    if(!active||!nav.getClientRects().length)return;
    const box=active.getBoundingClientRect(),parent=nav.getBoundingClientRect();
    if(!box.width||!box.height)return;
    rail.style.width=box.width+'px';rail.style.height=box.height+'px';
    rail.style.transform=`translate3d(${box.left-parent.left}px,${box.top-parent.top+nav.scrollTop}px,0)`;
    if(previous!==active){
      const name=active.dataset.tab||active.dataset.view;
      enterSection(document.getElementById('tab-'+name)||document.getElementById('view-'+name));
      play(active.querySelector('.ui-icon'),[{opacity:.55,transform:'scale(.8)'},{opacity:1,transform:'none'}],{duration:360});
      previous=active;
    }
    if(!nav.classList.contains('motion-nav-ready'))nav.classList.add('motion-nav-ready');
  }
  function queueNav(){if(!navFrame)navFrame=requestAnimationFrame(syncNav);}
  if(nav){
    rail=document.createElement('span');rail.className='motion-nav-rail';rail.setAttribute('aria-hidden','true');nav.prepend(rail);
    new MutationObserver(queueNav).observe(nav,{attributes:true,subtree:true,attributeFilter:['class','aria-current']});
    new ResizeObserver(queueNav).observe(nav);
    document.fonts?.ready.then(queueNav);queueNav();
  }
  /* A film-light pass connects navigation, the dashboard and the composer. */
  const main=document.querySelector('.app-main'),header=document.querySelector('.app-header');
  let section,chartPending=false,statsPending=false,visualFrame=0,lastVideo='';
  const scans=new WeakMap();
  let routeLight;
  if(header){routeLight=document.createElement('span');routeLight.className='motion-route-light';routeLight.setAttribute('aria-hidden','true');header.append(routeLight);}
  function scan(card){
    if(!card)return;
    let edge=scans.get(card);
    if(!edge||!edge.isConnected){edge=document.createElement('span');edge.className='motion-card-scan';edge.setAttribute('aria-hidden','true');card.classList.add('motion-material');card.append(edge);scans.set(card,edge);}
    play(edge,[{backgroundPosition:'200% 0',opacity:0},{offset:.16,opacity:1},{offset:.7,opacity:.85},{backgroundPosition:'-100% 0',opacity:0}],{duration:1100});
  }
  function reveal(nodes){
    [...nodes].filter(node=>node.getClientRects().length).slice(0,5).forEach((node,index)=>play(node,
      [{opacity:.55,transform:'translate3d(0,12px,0)',clipPath:'inset(0 0 2% 0 round 12px)'},{opacity:1,transform:'none',clipPath:'inset(0 round 12px)'}],
      {duration:440,delay:Math.min(index*35,120)}));
  }
  function enterSection(next){
    if(section&&section!==next)animations.forEach((animation,node)=>{if(section.contains(node))animation.cancel();});
    section=next;if(!section)return;
    play(document.querySelector('#pageTitle'),[{opacity:.4,transform:'translate3d(0,5px,0)'},{opacity:1,transform:'none'}],{duration:300});
    play(routeLight,[{transform:'translateX(-100%)',opacity:0},{offset:.2,opacity:1},{offset:.75,opacity:.8},{transform:'translateX(350%)',opacity:0}],{duration:720});
    if(section.id==='tab-dash'){statsPending=chartPending=enabled();reveal(section.querySelectorAll(':scope>.dashboard-grid,:scope>.card'));}
    else{reveal(section.querySelectorAll(':scope>.card,:scope>.compose-layout>.card,:scope>.generator-grid>.card,:scope>.gallery-layout>.card,:scope>.creation-grid>.card'));}
    if(section.id==='tab-gallery'||section.id==='view-create')scan(section.querySelector('textarea')?.closest('.card'));
    queueVisuals();
  }
  function inspectVisuals(){
    visualFrame=0;
    if(!enabled())return;
    if(section?.id==='tab-dash'){
      const stats=document.querySelectorAll('#statCards>.stat');
      if(statsPending&&stats.length>1){statsPending=false;reveal(stats);}
      const bars=document.querySelectorAll('#chart .bar');
      if(chartPending&&bars.length){chartPending=false;bars.forEach((bar,index)=>play(bar,
        [{transform:'scaleY(0)',opacity:.25,transformOrigin:'50% 100%'},{transform:'scaleY(1)',opacity:1,transformOrigin:'50% 100%'}],
        {duration:640,delay:Math.min(Math.floor(index/2)*38,230)}));}
    }
    main?.querySelectorAll('.loading,.motion-loading,#genStatus,#memberStatus').forEach(node=>{
      const busy=/^正在(?:加载|查询|同步|读取|提交|上传)/.test(node.textContent.trim())&&!node.querySelector('table,dl,video');
      node.classList.toggle('motion-loading',busy);
    });
    const result=document.querySelector('#adminResult'),video=result?.querySelector('video');
    const source=video?.getAttribute('src')||'';
    if(source&&source!==lastVideo){lastVideo=source;if(section?.id==='tab-gallery')scan(result.closest('.card'));}
  }
  function queueVisuals(){if(!visualFrame&&enabled())visualFrame=requestAnimationFrame(inspectVisuals);}
  if(main)new MutationObserver(queueVisuals).observe(main,{childList:true,subtree:true});
  document.addEventListener('toggle',event=>{
    const details=event.target;if(!details.matches('details')||!details.open||!main?.contains(details))return;
    [...details.children].filter(node=>node.tagName!=='SUMMARY').forEach(node=>play(node,
      [{opacity:.35,clipPath:'inset(0 0 35% 0)'},{opacity:1,clipPath:'inset(0)'}],{duration:280}));
  },true);
  const fine=matchMedia('(hover:hover) and (pointer:fine)');
  let light,point,lightFrame=0;
  const spotSelector='.stat,.dashboard-grid>.card,.motion-material';
  function clearLight(){if(light){light.classList.remove('motion-lit');light=null;}if(lightFrame)cancelAnimationFrame(lightFrame);lightFrame=0;}
  function drawLight(){lightFrame=0;if(!light||!point||!enabled())return;const box=light.getBoundingClientRect();light.style.setProperty('--motion-x',(point.x-box.left)+'px');light.style.setProperty('--motion-y',(point.y-box.top)+'px');}
  document.addEventListener('pointermove',event=>{
    if(!fine.matches||!enabled()||root.dataset.hoverGlow==='off')return;
    const card=event.target.closest(spotSelector);
    if(card!==light){clearLight();light=card;if(light)light.classList.add('motion-lit');}
    if(!light)return;point={x:event.clientX,y:event.clientY};if(!lightFrame)lightFrame=requestAnimationFrame(drawLight);
  },{passive:true});
  document.documentElement.addEventListener('pointerleave',clearLight);
  document.addEventListener('click',event=>{
    const button=event.target.closest('.btn,.primary,.text-button');
    if(!button||button.disabled||button.getAttribute('aria-disabled')==='true')return;
    const icon=button.querySelector('.ui-icon');
    if(button.matches('[aria-label="手动刷新"]'))play(icon,[{transform:'rotate(0deg)'},{transform:'rotate(360deg)'}],{duration:550});
    else play(icon,[{opacity:.5},{opacity:1}],{duration:180});
  });
  /* Opening only: an updated drawer body or refreshed table never replays an entrance. */
  const visible=new WeakMap();
  function inspectOverlay(node,initial=false){
    const shown=!node.hidden&&getComputedStyle(node).display!=='none';
    if(!initial&&shown&&!visible.get(node)){
      const drawer=node.classList.contains('detail-drawer'),target=drawer?node:node.querySelector('.box')||node;
      play(target,[{opacity:0,transform:drawer?'translate3d(18px,0,0)':'translate3d(0,10px,0) scale(.985)'},{opacity:1,transform:'none'}],{duration:drawer?300:220});
    }
    visible.set(node,shown);
  }
  function registerOverlay(node,entering=false){
    if(visible.has(node))return;visible.set(node,false);inspectOverlay(node,!entering);
    new MutationObserver(()=>inspectOverlay(node)).observe(node,{attributes:true,attributeFilter:['hidden','style','class']});
  }
  document.querySelectorAll('.detail-drawer,.dialog,.mask').forEach(registerOverlay);
  new MutationObserver(records=>records.forEach(record=>record.addedNodes.forEach(node=>{
    if(node.nodeType!==1)return;
    if(node.matches('.detail-drawer,.dialog,.mask'))registerOverlay(node,true);
    node.querySelectorAll('.detail-drawer,.dialog,.mask').forEach(registerOverlay);
  }))).observe(document.body,{childList:true});
  function pause(){root.dataset.motionVisibility=document.hidden?'hidden':'visible';if(!enabled()){animations.forEach(animation=>animation.cancel());animations.clear();clearLight();chartPending=statsPending=false;if(visualFrame)cancelAnimationFrame(visualFrame);visualFrame=0;}queueNav();queueVisuals();}
  document.addEventListener('dola-appearance-change',pause);document.addEventListener('visibilitychange',pause);
  pause();
})();
