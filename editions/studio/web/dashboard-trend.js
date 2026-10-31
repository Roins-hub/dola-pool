/* Daily counts remain unchanged; this module only draws and inspects them. */
window.DashboardTrend=(()=>{
  'use strict';
  const states=new WeakMap(),watched=new WeakSet(),ns='http://www.w3.org/2000/svg';
  const visibility=typeof IntersectionObserver==='function'?new IntersectionObserver(entries=>{
    for(const entry of entries)entry.target.dataset.motionVisible=String(entry.isIntersecting);
  }):null;
  function pageVisibility(){document.documentElement.dataset.trendPageVisible=String(!document.hidden);}
  document.addEventListener('visibilitychange',pageVisibility);pageVisibility();
  let nextId=0;
  const count=value=>{const n=Number(value);return Number.isFinite(n)&&n>=0?n:0;};
  const number=value=>new Intl.NumberFormat('zh-CN').format(value);
  function svgNode(tag,attributes={}){const el=document.createElementNS(ns,tag);for(const [key,value] of Object.entries(attributes))el.setAttribute(key,String(value));return el;}
  function node(tag,className,text){const el=document.createElement(tag);el.className=className;if(text!==undefined)el.textContent=text;return el;}
  // Horizontal tangents keep every segment inside its two real daily values.
  function curve(points){return points.map((p,i)=>{if(!i)return `M${p.x},${p.y}`;const a=points[i-1],mid=(a.x+p.x)/2;return `C${mid},${a.y} ${mid},${p.y} ${p.x},${p.y}`;}).join(' ');}
  function render(target,input){
    const host=typeof target==='string'?document.querySelector(target):target;if(!host)return;
    if(!watched.has(host)){watched.add(host);host.dataset.motionVisible=String(!visibility);visibility?.observe(host);}
    const days=(Array.isArray(input)?input:[]).map(d=>({day:String(d.day??''),completed:count(d.completed),failed:count(d.failed)}));
    const signature=JSON.stringify(days),previous=states.get(host);if(previous?.signature===signature)return;
    const focused=host.contains(document.activeElement)?document.activeElement?.dataset.day:null;
    const selectedDay=previous?.selectedDay;
    const state={signature,selectedDay:null};states.set(host,state);
    host.classList.add('aurora-chart');host.replaceChildren();
    const max=Math.max(0,...days.flatMap(d=>[d.completed,d.failed]));
    const scale=document.querySelector('#chartScale');if(scale)scale.textContent=days.length?'最高 '+number(max)+' 个 / 日':'';
    if(!days.length){host.append(node('div','empty','暂无趋势记录'));return;}
    const ceiling=Math.max(2,Math.ceil(max/2)*2),top=12,bottom=172,height=bottom-top;
    const plot=node('div','trend-plot');host.append(plot);
    const grid=node('div','trend-grid');
    [ceiling,ceiling/2,0].forEach((value,i)=>{const tick=node('div','trend-tick');tick.style.top=(top+i*height/2)+'px';tick.append(node('span','',number(value)));grid.append(tick);});plot.append(grid);
    const svg=svgNode('svg',{viewBox:'0 0 1000 184',preserveAspectRatio:'none','aria-hidden':'true',class:'trend-svg'}),defs=svgNode('defs');svg.append(defs);
    const id='daily-aurora-'+(++nextId);
    const series=[{key:'completed',colors:['#83d9ba','#82e5ed','#94bafa']},{key:'failed',colors:['#f7a6b8','#dba8e6','#f3a0ad']}];
    const points={};
    for(const {key,colors} of series){
      const gradient=svgNode('linearGradient',{id:id+'-'+key,x1:'0%',x2:'100%',y1:'0%',y2:'0%'});
      colors.forEach((color,i)=>gradient.append(svgNode('stop',{offset:(i*50)+'%','stop-color':color})));defs.append(gradient);
      points[key]=days.map((d,i)=>({x:1000*(i+.5)/days.length,y:bottom-(d[key]/ceiling)*height}));
      // A single day still has a visible, short line at its actual value.
      const coordinates=days.length===1?[{x:480,y:points[key][0].y},{x:520,y:points[key][0].y}]:points[key];
      const path=curve(coordinates);
      for(const kind of ['glow','line'])svg.append(svgNode('path',{d:path,fill:'none',stroke:`url(#${id}-${key})`,class:`trend-${kind} trend-${key}`,'vector-effect':'non-scaling-stroke'}));
      // Only light travels. The data path and its zero baseline never move.
      for(const kind of ['flow-halo','flow'])svg.append(svgNode('path',{d:path,fill:'none',stroke:`url(#${id}-${key})`,class:`trend-${kind} trend-${key}`,pathLength:100,'vector-effect':'non-scaling-stroke'}));
    }
    plot.append(svg);
    const guide=node('div','trend-guide');guide.hidden=true;plot.append(guide);
    const dots=series.map(({key})=>{const dot=node('span','trend-point trend-point-'+key);dot.hidden=true;plot.append(dot);return dot;});
    const tooltip=node('div','trend-tooltip');tooltip.id=id+'-tooltip';tooltip.setAttribute('role','tooltip');tooltip.hidden=true;
    const date=node('strong','trend-tooltip-date'),complete=node('div','trend-tooltip-row'),failed=node('div','trend-tooltip-row');
    complete.append(node('span','trend-complete-label','完成'),node('b','',''));failed.append(node('span','trend-failed-label','失败'),node('b','',''));
    tooltip.append(date,complete,failed);plot.append(tooltip);
    const buttons=node('div','trend-days');buttons.setAttribute('role','group');buttons.setAttribute('aria-label','每日任务数量，左右方向键切换日期');plot.append(buttons);
    function inspect(index){
      const d=days[index];if(!d)return;state.selectedDay=d.day;
      const x=100*(index+.5)/days.length;
      guide.hidden=false;guide.style.left=x+'%';tooltip.hidden=false;tooltip.style.setProperty('--point-x',x+'%');date.textContent=d.day;
      complete.lastElementChild.textContent=number(d.completed);failed.lastElementChild.textContent=number(d.failed);
      series.forEach(({key},j)=>{dots[j].hidden=false;dots[j].style.left=x+'%';dots[j].style.top=points[key][index].y+'px';});
      [...buttons.children].forEach((button,i)=>{button.classList.toggle('is-active',i===index);if(i===index)button.setAttribute('aria-describedby',tooltip.id);else button.removeAttribute('aria-describedby');});
    }
    function hide(){if(buttons.contains(document.activeElement))return;state.selectedDay=null;guide.hidden=tooltip.hidden=true;dots.forEach(dot=>dot.hidden=true);[...buttons.children].forEach(b=>{b.classList.remove('is-active');b.removeAttribute('aria-describedby');});}
    days.forEach((d,i)=>{
      const button=node('button','trend-day');button.type='button';button.dataset.day=d.day;button.setAttribute('aria-label',`${d.day}，完成 ${number(d.completed)}，失败 ${number(d.failed)}`);button.append(node('span','trend-date',d.day.slice(-5)));
      // A stationary pointer must not override keyboard focus after a refresh.
      button.addEventListener('pointermove',()=>inspect(i));button.addEventListener('focus',()=>inspect(i));button.addEventListener('click',()=>inspect(i));
      button.addEventListener('keydown',event=>{
        if(event.key==='Escape'){button.blur();hide();return;}
        let next=i;if(event.key==='ArrowLeft')next=Math.max(0,i-1);else if(event.key==='ArrowRight')next=Math.min(days.length-1,i+1);else if(event.key==='Home')next=0;else if(event.key==='End')next=days.length-1;else return;
        event.preventDefault();buttons.children[next].focus();
      });buttons.append(button);
    });
    plot.addEventListener('pointerleave',hide);buttons.addEventListener('focusout',event=>{if(!buttons.contains(event.relatedTarget))queueMicrotask(hide);});
    if(focused){const i=days.findIndex(d=>d.day===focused);if(i>=0)buttons.children[i].focus({preventScroll:true});}
    else if(selectedDay){const i=days.findIndex(d=>d.day===selectedDay);if(i>=0)inspect(i);}
  }
  return {render};
})();
