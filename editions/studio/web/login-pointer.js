(()=>{
  'use strict';
  const stage=document.querySelector('#stage'),scene=stage?.querySelector('.scene');
  if(!document.body.classList.contains('login-deep-sea')||!stage||!scene)return;
  const reduced=matchMedia('(prefers-reduced-motion:reduce)'),fine=matchMedia('(hover:hover) and (pointer:fine)'),compact=matchMedia('(max-width:700px)'),pause=document.querySelector('#pauseBtn');
  const reveal=document.createElement('div');reveal.className='deep-sea-reveal';reveal.setAttribute('aria-hidden','true');reveal.dataset.revealing='false';scene.after(reveal);
  const canvas=document.createElement('canvas');canvas.className='deep-sea-aurora';canvas.setAttribute('aria-hidden','true');canvas.dataset.running='false';canvas.dataset.points='0';stage.querySelector('.scene-tone').after(canvas);
  const ctx=canvas.getContext('2d',{alpha:true});
  if(!ctx)return;
  const LIFETIME=1150,MAX_POINTS=120;
  let frame=0,points=[],lastPointer=null,inside=false,allowed=false,width=1,height=1,dpr=1;
  let x=0,y=0,targetX=0,targetY=0,radius=0,targetRadius=0,lastTime=0;

  function reason(){if(!stage.classList.contains('theme-2'))return'other-concept';if(document.hidden)return'hidden';if(reduced.matches)return'reduced';if(compact.matches||!fine.matches)return'touch';if(pause?.getAttribute('aria-pressed')==='true')return'paused';if(!['cover','form'].includes(stage.dataset.state))return'inactive-phase';return'ready';}
  function clear(){if(frame)cancelAnimationFrame(frame);frame=0;points=[];lastPointer=null;inside=false;radius=targetRadius=0;lastTime=0;ctx.clearRect(0,0,width,height);reveal.dataset.revealing='false';reveal.style.removeProperty('--sea-radius');canvas.dataset.running='false';canvas.dataset.points='0';}
  function sync(reset=false){const previousReason=canvas.dataset.mode,nextReason=reason(),nextAllowed=nextReason==='ready';if(reset||!nextAllowed||nextAllowed!==allowed)clear();allowed=nextAllowed;canvas.dataset.mode=nextReason;if(nextReason==='touch'||nextReason==='reduced')scene.querySelector('video')?.pause();else if(nextReason==='ready'&&previousReason==='touch')scene.querySelector('video')?.play().catch(()=>{});}
  function resize(){clear();const rect=stage.getBoundingClientRect();width=Math.max(1,rect.width);height=Math.max(1,rect.height);dpr=Math.min(devicePixelRatio||1,1.5);canvas.width=Math.round(width*dpr);canvas.height=Math.round(height*dpr);ctx.setTransform(dpr,0,0,dpr,0,0);sync();}
  function start(){if(!allowed||frame)return;canvas.dataset.running='true';lastTime=performance.now();frame=requestAnimationFrame(tick);}
  function pointAt(event){const rect=stage.getBoundingClientRect();return{x:event.clientX-rect.left,y:event.clientY-rect.top,t:performance.now()};}
  function move(event){if(!allowed||event.pointerType==='touch')return;const p=pointAt(event);inside=true;targetX=p.x;targetY=p.y;targetRadius=Math.max(160,Math.min(205,width*.148));
    if(!lastPointer){x=targetX;y=targetY;lastPointer=p;points.push({...p,speed:0});}
    else{const distance=Math.hypot(p.x-lastPointer.x,p.y-lastPointer.y),dt=Math.max(8,p.t-lastPointer.t),speed=Math.min(2.8,distance/dt);if(distance>=2){const steps=Math.min(18,Math.max(1,Math.ceil(distance/9)));for(let i=1;i<=steps;i++){const a=i/steps;points.push({x:lastPointer.x+(p.x-lastPointer.x)*a,y:lastPointer.y+(p.y-lastPointer.y)*a,t:lastPointer.t+(p.t-lastPointer.t)*a,speed});}lastPointer=p;}else if(dt>80)lastPointer=p;}
    if(points.length>MAX_POINTS)points.splice(0,points.length-MAX_POINTS);start();
  }
  function leave(){inside=false;targetRadius=0;lastPointer=null;if(allowed)start();}
  function line(points){ctx.moveTo(points[0].x,points[0].y);for(let i=1;i<points.length-1;i++){const mid={x:(points[i].x+points[i+1].x)/2,y:(points[i].y+points[i+1].y)/2};ctx.quadraticCurveTo(points[i].x,points[i].y,mid.x,mid.y);}const last=points[points.length-1];ctx.lineTo(last.x,last.y);}
  function ribbon(nodes,scale,offset,color,blur=0){if(nodes.length<3)return;const upper=[],lower=[];nodes.forEach((p,i)=>{const prev=nodes[Math.max(0,i-1)],next=nodes[Math.min(nodes.length-1,i+1)],dx=next.x-prev.x,dy=next.y-prev.y,len=Math.max(1,Math.hypot(dx,dy)),nx=-dy/len,ny=dx/len;const end=Math.sin(Math.PI*(i+.3)/(nodes.length+.1))**.55,w=(12+p.speed*13)*.7*scale*p.life*end;const fold=(Math.sin(p.age*5.5+i*.15)*6+offset)*.7*p.life;upper.push({x:p.x+nx*(w+fold),y:p.y+ny*(w+fold)});lower.push({x:p.x+nx*(-w+fold),y:p.y+ny*(-w+fold)});});ctx.beginPath();line(upper);const reverse=lower.reverse();ctx.lineTo(reverse[0].x,reverse[0].y);for(let i=1;i<reverse.length-1;i++){const mid={x:(reverse[i].x+reverse[i+1].x)/2,y:(reverse[i].y+reverse[i+1].y)/2};ctx.quadraticCurveTo(reverse[i].x,reverse[i].y,mid.x,mid.y);}ctx.lineTo(reverse[reverse.length-1].x,reverse[reverse.length-1].y);ctx.closePath();ctx.fillStyle=color;ctx.shadowColor='#63e6ec';ctx.shadowBlur=blur*.7;ctx.fill();ctx.shadowBlur=0;}
  function draw(now){ctx.clearRect(0,0,width,height);points=points.filter(p=>now-p.t<LIFETIME);canvas.dataset.points=String(points.length);if(points.length<3)return;
    const nodes=points.map(p=>{const age=(now-p.t)/LIFETIME,life=(1-age)**1.5;return{...p,age,life,x:p.x+Math.sin(age*4.6+p.x*.006)*age*3.5,y:p.y-age*8.4};});
    const first=nodes[0],last=nodes[nodes.length-1],gradient=ctx.createLinearGradient(first.x,first.y,last.x+.01,last.y+.01);
    gradient.addColorStop(0,'rgba(133,111,255,0)');gradient.addColorStop(.25,'rgba(116,131,255,.16)');gradient.addColorStop(.55,'rgba(70,225,244,.33)');gradient.addColorStop(.82,'rgba(149,255,226,.43)');gradient.addColorStop(1,'rgba(117,220,255,.12)');
    ctx.globalCompositeOperation='lighter';ribbon(nodes,1.1,0,'rgba(61,183,255,.07)',26);ribbon(nodes,.58,-3,gradient,12);ribbon(nodes,.22,8,'rgba(133,252,225,.26)',5);ribbon(nodes,.10,-13,'rgba(123,130,255,.20)',4);ctx.globalCompositeOperation='source-over';
  }
  function tick(now){frame=0;if(!allowed){clear();return;}const dt=Math.min(50,now-lastTime||16);lastTime=now;const ease=1-Math.exp(-dt/62),fade=1-Math.exp(-dt/(inside?90:115));x+=(targetX-x)*ease;y+=(targetY-y)*ease;radius+=(targetRadius-radius)*fade;if(radius<.4)radius=0;
    reveal.dataset.revealing=String(radius>0);if(radius>0){reveal.style.setProperty('--sea-x',`${x.toFixed(2)}px`);reveal.style.setProperty('--sea-y',`${y.toFixed(2)}px`);reveal.style.setProperty('--sea-radius',`${radius.toFixed(2)}px`);}draw(now);
    const unsettled=Math.abs(targetX-x)>.2||Math.abs(targetY-y)>.2||Math.abs(targetRadius-radius)>.35;
    if(points.length||unsettled){frame=requestAnimationFrame(tick);canvas.dataset.running='true';}else canvas.dataset.running='false';
  }
  stage.addEventListener('pointermove',move,{passive:true});stage.addEventListener('pointerleave',leave,{passive:true});stage.addEventListener('pointercancel',()=>{clear();sync();},{passive:true});
  new MutationObserver(records=>{const reset=records.some(r=>r.attributeName==='class'||(r.attributeName==='data-state'&&stage.dataset.state==='cover'));sync(reset);}).observe(stage,{attributes:true,attributeFilter:['class','data-state']});
  if(pause)new MutationObserver(()=>sync()).observe(pause,{attributes:true,attributeFilter:['aria-pressed']});
  document.addEventListener('visibilitychange',()=>sync(document.hidden));reduced.addEventListener('change',()=>sync(true));fine.addEventListener('change',()=>sync(true));compact.addEventListener('change',()=>sync(true));window.addEventListener('pagehide',()=>{allowed=false;clear();});window.addEventListener('pageshow',()=>sync(true));new ResizeObserver(resize).observe(stage);resize();
})();
