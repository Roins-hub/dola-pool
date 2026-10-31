/* Shared UI primitives. No network access or role-dependent data lives here. */
window.UI=(()=>{
  const paths={home:'<path d="m3 10 9-7 9 7v10a1 1 0 0 1-1 1h-5v-7H9v7H4a1 1 0 0 1-1-1z"/>',tasks:'<rect x="4" y="3" width="16" height="18" rx="2"/><path d="M8 8h8M8 12h8M8 16h5"/>',video:'<rect x="3" y="5" width="18" height="14" rx="2"/><path d="m10 9 5 3-5 3z"/>',accounts:'<circle cx="9" cy="7" r="3"/><path d="M3 21v-3a6 6 0 0 1 12 0v3M16 4a3 3 0 0 1 0 6M21 21v-3a6 6 0 0 0-4-5.6"/>',proxy:'<circle cx="5" cy="12" r="3"/><circle cx="18" cy="5" r="3"/><circle cx="18" cy="19" r="3"/><path d="m8 10 7-4M8 14l7 4"/>',key:'<circle cx="8" cy="8" r="5"/><path d="m12 12 9 9M16 16l3-3M19 19l3-3"/>',users:'<circle cx="12" cy="8" r="4"/><path d="M4 22v-2a8 8 0 0 1 16 0v2"/>',book:'<path d="M4 3h6a2 2 0 0 1 2 2v16a3 3 0 0 0-3-2H4zM20 3h-6a2 2 0 0 0-2 2v16a3 3 0 0 1 3-2h5z"/>',refresh:'<path d="M20 7v5h-5M4 17v-5h5M6 6a8 8 0 0 1 13 1M18 18a8 8 0 0 1-13-1"/>',logout:'<path d="M9 4H4v16h5M13 8l4 4-4 4M8 12h13"/>',menu:'<path d="M4 6h16M4 12h16M4 18h16"/>',chevron:'<path d="m9 5 7 7-7 7"/>',close:'<path d="m6 6 12 12M18 6 6 18"/>',check:'<path d="m5 12 4 4L19 6"/>',alert:'<circle cx="12" cy="12" r="9"/><path d="M12 7v6M12 17h.01"/>',copy:'<rect x="8" y="8" width="12" height="13" rx="2"/><path d="M16 8V3H3v13h5"/>',clock:'<circle cx="12" cy="12" r="9"/><path d="M12 7v5l3 2"/>',search:'<circle cx="10" cy="10" r="7"/><path d="m15 15 6 6"/>',plus:'<path d="M12 4v16M4 12h16"/>',download:'<path d="M12 3v12m-5-5 5 5 5-5M4 16v5h16v-5"/>',empty:'<path d="M4 8 7 3h10l3 5v13H4zM4 12h5l1 3h4l1-3h5"/>'};
  const esc=v=>String(v??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  const icon=name=>`<svg class="ui-icon" viewBox="0 0 24 24" aria-hidden="true">${paths[name]||paths.tasks}</svg>`;
  const number=n=>n==null?'—':new Intl.NumberFormat('zh-CN').format(n);
  const time=t=>t?new Intl.DateTimeFormat('zh-CN',{month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit',hour12:false}).format(new Date(t*1000)):'—';
  const safeURL=value=>{if(!value||!String(value).trim())return '';try{const u=new URL(value,location.origin);return /^https?:$/.test(u.protocol)?u.href:'';}catch{return '';}};
  function detailError(value,depth=0){
    if(depth>5)return '请求失败，请稍后重试';
    if(Array.isArray(value))return value.map(x=>detailError(x?.msg||x,depth+1)).join('；')||'参数不正确';
    if(value&&typeof value==='object'){const message=value.message||value.msg||value.detail;return message?detailError(message,depth+1):'请求失败，请稍后重试';}
    return String(value||'请求失败，请稍后重试');
  }
  function empty(title,description=''){return `<div class="empty">${icon('empty')}<strong>${esc(title)}</strong>${esc(description)}</div>`;}
  function toast(message,error=false){let stack=document.getElementById('toastStack');if(!stack){stack=document.createElement('div');stack.id='toastStack';stack.className='toast-stack';stack.setAttribute('aria-live','polite');document.body.append(stack);}const node=document.createElement('div');node.className='toast'+(error?' is-error':'');node.innerHTML=icon(error?'alert':'check')+`<span>${esc(message)}</span>`;stack.append(node);setTimeout(()=>node.remove(),4500);}
  async function copy(text){try{await navigator.clipboard.writeText(text);toast('已复制');return true;}catch{const area=document.createElement('textarea');area.value=text;area.style.position='fixed';area.style.opacity='0';document.body.append(area);area.select();let ok=false;try{ok=document.execCommand('copy');}finally{area.remove();}toast(ok?'已复制':'复制失败，请选中文字后复制',!ok);return ok;}}
  const renderedHTML=new WeakMap();
  function matchingControl(fragment,control,node){
    const candidates=[...fragment.querySelectorAll(control.tagName.toLowerCase())];
    if(control.id)return candidates.find(x=>x.id===control.id);
    const identity=[...control.attributes].filter(x=>x.name.startsWith('data-'));
    if(identity.length)return candidates.find(x=>identity.every(a=>x.getAttribute(a.name)===a.value));
    const action=control.getAttribute('onclick');if(action)return candidates.find(x=>x.getAttribute('onclick')===action);
    const name=control.getAttribute('aria-label');if(name)return candidates.find(x=>x.getAttribute('aria-label')===name);
    if(control.classList.contains('table-scroll'))return [...fragment.querySelectorAll('.table-scroll')][[...node.querySelectorAll('.table-scroll')].indexOf(control)];
    return candidates.find(x=>x.textContent===control.textContent&&x.getAttribute('href')===control.getAttribute('href'));
  }
  function render(target,html){
    const node=typeof target==='string'?document.querySelector(target):target;if(!node||renderedHTML.get(node)===html)return;
    const scrolls=[...node.querySelectorAll('.table-scroll')].map(x=>[x.scrollLeft,x.scrollTop]),opened=[...node.querySelectorAll('details[open][data-group]')].map(x=>x.dataset.group);
    const active=document.activeElement,drawer=document.getElementById('detailDrawer'),control=node.contains(active)?active:drawer&&!drawer.hidden&&drawerOrigin&&node.contains(drawerOrigin)?drawerOrigin:null;
    const template=document.createElement('template');template.innerHTML=html;
    let retained=false;
    if(control){const replacement=matchingControl(template.content,control,node);if(replacement){
      const value=control.matches('input,textarea,select')?control.value:undefined,start=control.selectionStart,end=control.selectionEnd;
      [...control.attributes].forEach(a=>{if(!replacement.hasAttribute(a.name))control.removeAttribute(a.name);});[...replacement.attributes].forEach(a=>control.setAttribute(a.name,a.value));
      control.replaceChildren(...replacement.childNodes);if(value!==undefined)control.value=value;
      replacement.replaceWith(control);retained=true;
      if(typeof start==='number')try{control.setSelectionRange(start,end);}catch{}
    }}
    node.replaceChildren(template.content);renderedHTML.set(node,html);
    node.querySelectorAll('.table-scroll').forEach((x,i)=>{if(scrolls[i]){x.scrollLeft=scrolls[i][0];x.scrollTop=scrolls[i][1];}});node.querySelectorAll('details[data-group]').forEach(x=>{x.open=opened.includes(x.dataset.group);});
    if(control===active){if(retained)control.focus({preventScroll:true});else node.querySelector('.table-scroll,button,input,select,a[href]')?.focus({preventScroll:true});}
  }
  let drawerOrigin=null;
  function openDrawer(title,body){let drawer=document.getElementById('detailDrawer');if(!drawer){drawer=document.createElement('aside');drawer.id='detailDrawer';drawer.className='detail-drawer';drawer.setAttribute('aria-labelledby','drawerTitle');drawer.innerHTML='<div class="drawer-heading"><h2 id="drawerTitle"></h2><button class="btn icon-button" type="button" aria-label="关闭详情">'+icon('close')+'</button></div><div class="drawer-body"></div>';drawer.querySelector('button').onclick=closeDrawer;document.body.append(drawer);}if(drawer.hidden||!drawer.contains(document.activeElement)||!drawerOrigin?.isConnected)drawerOrigin=document.activeElement;delete drawer.dataset.requestKey;drawer.querySelector('#drawerTitle').textContent=title;drawer.querySelector('.drawer-body').innerHTML=body;drawer.hidden=false;drawer.querySelector('button').focus();return drawer;}
  function closeDrawer(){const drawer=document.getElementById('detailDrawer');if(!drawer||drawer.hidden)return;drawer.hidden=true;if(drawerOrigin?.isConnected)drawerOrigin.focus({preventScroll:true});else document.querySelector('main .table-scroll,main button')?.focus({preventScroll:true});}
  function setError(message,retry){const node=document.getElementById('pageError');if(!node)return;node.hidden=!message;if(message){node.innerHTML=`<span>${icon('alert')} ${esc(message)}</span><button type="button" class="btn">重试</button>`;node.querySelector('button').onclick=retry;}}
  function navigation(open=false){const sidebar=document.querySelector('.sidebar'),toggle=document.getElementById('sideToggle'),mobile=matchMedia('(max-width:767px)').matches,wasOpen=document.body.classList.contains('nav-open');document.body.classList.toggle('nav-open',open&&mobile);toggle?.setAttribute('aria-expanded',String(open&&mobile));if(sidebar){sidebar.inert=mobile&&!open;if(mobile&&!open)sidebar.setAttribute('aria-hidden','true');else sidebar.removeAttribute('aria-hidden');}if(open&&mobile)sidebar?.querySelector('nav button')?.focus();else if(wasOpen)toggle?.focus();}
  function init(){document.querySelectorAll('[data-icon]').forEach(node=>node.innerHTML=icon(node.dataset.icon));const toggle=document.getElementById('sideToggle');if(toggle)toggle.onclick=()=>navigation(!document.body.classList.contains('nav-open'));navigation();matchMedia('(max-width:767px)').addEventListener('change',()=>navigation());document.getElementById('sidebarOverlay')?.addEventListener('click',()=>navigation());document.querySelectorAll('.sidebar nav button').forEach(node=>node.addEventListener('click',()=>navigation()));document.addEventListener('keydown',e=>{if(e.key==='Escape'){navigation();closeDrawer();document.querySelectorAll('.dialog').forEach(node=>{if(node.style.display!=='none'){if(node.id==='videoModal'&&typeof window.closeVideo==='function')window.closeVideo();else node.style.display='none';}});}});document.querySelectorAll('.dialog').forEach(node=>{node.setAttribute('role','dialog');node.setAttribute('aria-modal','true');let origin;new MutationObserver(()=>{if(node.style.display==='none'){if(origin?.isConnected)origin.focus();return;}origin=document.activeElement;const title=node.querySelector('h2');if(title){title.id=node.id+'Title';node.setAttribute('aria-labelledby',title.id);}setTimeout(()=>node.querySelector('input,select,textarea,button')?.focus(),0);}).observe(node,{attributes:true,attributeFilter:['style']});node.addEventListener('keydown',e=>{if(e.key!=='Tab')return;const elements=[...node.querySelectorAll('button,input,select,textarea,a[href]')].filter(x=>!x.disabled&&x.getClientRects().length);if(!elements.length)return;const first=elements[0],last=elements.at(-1);if(e.shiftKey&&document.activeElement===first){e.preventDefault();last.focus();}else if(!e.shiftKey&&document.activeElement===last){e.preventDefault();first.focus();}});});}
  return {esc,icon,number,time,safeURL,detailError,empty,toast,copy,render,openDrawer,closeDrawer,setError,init,closeNavigation:()=>navigation()};
})();
