/* Authentication stays independent of the decorative login scene. */
(()=>{
  'use strict';
  const TOKEN_KEY='dola_token', REQUEST_TIMEOUT=15000;
  const q=id=>document.getElementById(id), form=q('authForm');
  if(!form)return;
  const scene=window.LoginScene;
  let mode='login', status={needs_setup:false,registration_open:false,admin_key_mode:false};
  let pending=false, revision=0, sessionUser=null, statusLoading=false, statusMessage='';
  function token(){try{return localStorage.getItem(TOKEN_KEY);}catch{return null;}}
  function forgetToken(expected){try{if(localStorage.getItem(TOKEN_KEY)===expected)localStorage.removeItem(TOKEN_KEY);}catch{/* A blocked store has no usable persisted session. */}}
  function saveToken(value){
    try{localStorage.setItem(TOKEN_KEY,value);if(localStorage.getItem(TOKEN_KEY)!==value)throw new Error('storage');}
    catch{throw new Error('浏览器无法保存登录状态，请允许此网站使用本地存储后重试。');}
  }
  function validUser(user){return user&&typeof user==='object'&&['admin','member'].includes(user.role);}
  function clearMsg(){q('err').hidden=true;q('err').textContent='';q('ok').hidden=true;q('ok').textContent='';}
  function showErr(message){q('err').textContent=message;q('err').hidden=false;q('ok').hidden=true;}
  function setBusy(value){
    pending=value;form.setAttribute('aria-busy',String(value));q('submitBtn').disabled=value;
    q('submitBtn').textContent=value?(mode==='register'?'正在创建…':'正在登录…'):(mode==='register'?'注册':'登录');
    document.querySelectorAll('#tabs button').forEach(button=>{button.disabled=value;});
    q('enterBtn').disabled=value;if(scene?.setBusy)scene.setBusy(value);
  }
  function showForm(){
    if(scene?.showForm)return scene.showForm();
    q('stage').dataset.state='form';q('loginPanel').hidden=false;q('loginPanel').inert=false;
    q('loginPanel').removeAttribute('aria-hidden');q('email').focus();return Promise.resolve();
  }
  function errorToForm(message){setBusy(false);showErr(message);if(scene?.error)scene.error();else showForm();}
  function apiError(data,fallback){
    if(typeof data?.detail==='string')return data.detail;
    if(Array.isArray(data?.detail))return data.detail.map(item=>item?.msg||'输入格式有误').join('；');return fallback;
  }
  async function api(path,options={}){
    const controller=new AbortController(),timer=setTimeout(()=>controller.abort(),REQUEST_TIMEOUT);
    const headers={'Content-Type':'application/json'},saved=token();if(saved)headers.Authorization='Bearer '+saved;
    try{
      const response=await fetch(path,{method:options.method||'GET',headers,signal:controller.signal,cache:'no-store',body:options.body?JSON.stringify(options.body):undefined});
      let data;try{data=await response.json();}catch{data=null;}
      if(!response.ok){const error=new Error(apiError(data,`请求失败（${response.status}），请稍后重试。`));error.status=response.status;throw error;}
      if(!data||typeof data!=='object')throw new Error('服务器返回了无效响应，请稍后重试。');return data;
    }catch(error){
      if(error.name==='AbortError')throw new Error('连接超时，请稍后重试。');
      if(error instanceof TypeError)throw new Error('无法连接服务器，请检查网络后重试。');throw error;
    }finally{clearTimeout(timer);}
  }
  function setMode(next,clear=true){
    if(pending)return;if(next==='register'&&!status.needs_setup&&!status.registration_open)return;
    mode=next==='register'?'register':'login';
    document.querySelectorAll('#tabs button').forEach(button=>{const selected=button.dataset.mode===mode;button.classList.toggle('on',selected);button.setAttribute('aria-pressed',String(selected));});
    q('title').textContent=status.needs_setup?'初始化管理员':mode==='login'?'欢迎回来':'创建账号';
    q('subtitle').textContent=status.needs_setup?'创建第一个账号，开始管理你的工作台':mode==='login'?'使用邮箱登录，继续你的创作':'创建成员账号，开始你的创作';
    q('submitBtn').textContent=mode==='login'?'登录':'注册';q('inviteRow').hidden=!(mode==='register'&&status.registration_open&&!status.needs_setup);
    q('password').autocomplete=mode==='login'?'current-password':'new-password';q('password').placeholder=mode==='login'?'输入你的密码':'至少 8 位';
    if(mode==='register')q('password').minLength=8;else q('password').removeAttribute('minlength');if(clear)clearMsg();
  }
  function applyStatus(){
    q('tabs').hidden=status.needs_setup||!status.registration_open;q('hint').hidden=true;q('hint').replaceChildren();
    if(status.needs_setup){q('hint').textContent='第一个注册的账号将成为管理员。请使用常用邮箱，并妥善保存密码。';q('hint').hidden=false;}
    else if(status.admin_key_mode){q('hint').append('仍可使用管理面板密码登录 ');const link=document.createElement('a');link.href='/admin';link.textContent='管理面板';q('hint').append(link,'。');q('hint').hidden=false;}
    if(!pending)setMode(status.needs_setup?'register':mode==='register'&&status.registration_open?'register':'login',false);
  }
  async function loadStatus(){
    if(statusLoading)return;statusLoading=true;q('formStatusRetry').disabled=true;
    try{
      const data=await api('/api/auth/status');status={needs_setup:data.needs_setup===true,registration_open:data.registration_open===true,admin_key_mode:data.admin_key_mode===true};
      applyStatus();q('formStatusRetry').hidden=true;
      if(statusMessage&&q('err').textContent===statusMessage)clearMsg();statusMessage='';
    }catch(error){statusMessage=error.message;if(!pending)showErr(error.message);q('formStatusRetry').hidden=false;}
    finally{statusLoading=false;q('formStatusRetry').disabled=false;}
  }
  async function restoreSession(saved,version){
    try{
      const data=await api('/api/me');if(!validUser(data.user))throw new Error('暂时无法确认账号状态，请重新登录。');
      if(version!==revision||token()!==saved)return;sessionUser=data.user;
      if(scene?.setEntryLabel)scene.setEntryLabel('继续进入');else q('enterBtn').querySelector('.entry-label').textContent='继续进入';
    }catch(error){
      if(version!==revision||token()!==saved)return;
      if(error.status===401||error.status===403)forgetToken(saved);
      // Temporary failures must not erase an otherwise valid saved session.
    }
  }
  async function navigate(user){
    if(!validUser(user))throw new Error('服务器未返回有效的账号信息，请重新登录。');
    const destination=user.role==='admin'?'/admin':'/member';
    if(scene?.enterWorkspace){try{await scene.enterWorkspace(destination);}catch{location.assign(destination);}}
    else location.assign(destination);
  }
  async function enter(){
    if(pending)return;if(!sessionUser){await showForm();return;}setBusy(true);
    try{await navigate(sessionUser);}catch(error){errorToForm(error.message);}
  }
  async function submit(event){
    event.preventDefault();if(pending)return;if(!form.reportValidity())return;
    const submittedMode=mode,body={email:q('email').value.trim(),password:q('password').value};
    if(submittedMode==='register'&&q('invite').value.trim())body.invite_code=q('invite').value.trim();
    revision++;sessionUser=null;clearMsg();setBusy(true);
    try{
      const data=await api(submittedMode==='login'?'/api/auth/login':'/api/auth/register',{method:'POST',body});
      if(typeof data.token!=='string'||!data.token.trim()||!validUser(data.user))throw new Error('登录响应不完整，请稍后重试。');
      saveToken(data.token);sessionUser=data.user;
      if(data.first_user){q('ok').textContent='管理员账号已创建，正在进入工作台…';q('ok').hidden=false;}
      await navigate(data.user);
    }catch(error){errorToForm(error.message);}
  }
  document.querySelectorAll('#tabs button').forEach(button=>button.addEventListener('click',()=>setMode(button.dataset.mode)));
  form.addEventListener('submit',submit);q('formStatusRetry').addEventListener('click',loadStatus);
  if(scene?.onEnter)scene.onEnter(enter);else q('enterBtn').addEventListener('click',enter);
  const saved=token();loadStatus();if(saved)restoreSession(saved,revision);
})();
