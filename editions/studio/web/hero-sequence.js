/* Generated film frames, decoded two at a time. Authentication is independent. */
(()=>{
  'use strict';
  const layer=document.querySelector('.login-art[data-sequence-manifest]');if(!layer)return;
  const root=document.documentElement,aside=layer.closest('.login-aside');
  const desktop=matchMedia('(min-width:901px)'),reduced=matchMedia('(prefers-reduced-motion:reduce)');
  const canvas=document.createElement('canvas');canvas.className='login-sequence';canvas.setAttribute('aria-hidden','true');layer.append(canvas);
  const context=canvas.getContext('2d',{alpha:true});if(!context)return;
  let manifest,blobs,loading=false,failed=false,visible=true,raf=0,decodeBusy=false,generation=0,last=-1,elapsed=0,started=0,bitmap;
  const active=()=>desktop.matches&&!reduced.matches&&visible&&!document.hidden&&root.dataset.theme==='dark'&&root.dataset.motion==='on'&&root.dataset.loginArt!=='off'&&aside.dataset.artPaused!=='true';
  function paint(image){
    const box=layer.getBoundingClientRect(),ratio=Math.min(devicePixelRatio||1,1.5),width=Math.round(box.width*ratio),height=Math.round(box.height*ratio);
    if(canvas.width!==width||canvas.height!==height){canvas.width=width;canvas.height=height;}
    context.clearRect(0,0,width,height);
    const scale=Math.max(width/image.width,height/image.height),drawnWidth=image.width*scale,drawnHeight=image.height*scale;
    context.drawImage(image,(width-drawnWidth)/2,(height-drawnHeight)/2,drawnWidth,drawnHeight);
  }
  function stop(){if(raf)cancelAnimationFrame(raf);raf=0;if(started){elapsed+=performance.now()-started;started=0;}generation++;}
  function tick(now){
    raf=0;if(!active()||!blobs){stop();return;}
    const frame=Math.floor(((elapsed+now-started)%manifest.duration_ms)/1000*manifest.fps)%blobs.length;
    if(frame!==last&&!decodeBusy){
      decodeBusy=true;const ticket=generation;
      createImageBitmap(blobs[frame]).then(next=>{
        if(ticket!==generation||!active()){next.close();return;}
        bitmap?.close();bitmap=next;paint(bitmap);last=frame;layer.dataset.sequence='ready';
      }).catch(()=>{failed=true;stop();layer.dataset.sequence='fallback';}).finally(()=>{decodeBusy=false;});
    }
    if(!failed)raf=requestAnimationFrame(tick);
  }
  async function load(){
    if(loading||blobs||failed||!active())return;loading=true;
    try{
      const response=await fetch(layer.dataset.sequenceManifest,{credentials:'omit',cache:'force-cache'});if(!response.ok)throw Error('manifest');
      const data=await response.json();
      if(!Array.isArray(data.frames)||data.frames.length<2||data.frames.length>90||data.duration_ms!==5000||!(data.fps>0&&data.fps<=18)||data.frames.length!==Math.round(data.duration_ms*data.fps/1000))throw Error('format');
      const base=new URL(layer.dataset.sequenceManifest,location.href),results=new Array(data.frames.length),pending=new Map();let cursor=0,total=0;
      async function getFrame(url){const res=await fetch(url,{credentials:'omit',cache:'force-cache'});if(!res.ok)throw Error('frame');const blob=await res.blob();total+=blob.size;if(total>6*1024*1024)throw Error('budget');return blob;}
      async function worker(){while(cursor<data.frames.length){const index=cursor++,url=new URL(data.frames[index],base);if(url.origin!==location.origin)throw Error('origin');if(!pending.has(url.href))pending.set(url.href,getFrame(url));results[index]=await pending.get(url.href);}}
      await Promise.all(Array.from({length:4},worker));manifest=data;blobs=results;
    }catch{failed=true;layer.dataset.sequence='fallback';}
    finally{loading=false;sync();}
  }
  function sync(){
    if(!active()){stop();return;}
    if(!blobs){load();return;}
    if(!raf&&!failed){started=performance.now();generation++;raf=requestAnimationFrame(tick);}
  }
  const intersection=new IntersectionObserver(entries=>{visible=entries[0].isIntersecting;sync();});intersection.observe(aside);
  new MutationObserver(sync).observe(aside,{attributes:true,attributeFilter:['data-art-paused']});
  document.addEventListener('dola-appearance-change',sync);document.addEventListener('visibilitychange',sync);
  desktop.addEventListener('change',sync);reduced.addEventListener('change',sync);
  new ResizeObserver(()=>{if(bitmap&&active())paint(bitmap);}).observe(layer);
  window.addEventListener('pagehide',()=>{stop();bitmap?.close();bitmap=null;last=-1;});window.addEventListener('pageshow',sync);
  if('requestIdleCallback' in window)requestIdleCallback(sync,{timeout:1800});else setTimeout(sync,600);
})();
