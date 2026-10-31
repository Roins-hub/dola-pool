/* Shared rendering for separately identified clips and request batches. */
window.TaskResultsUI=(()=>{
  const E=value=>UI.esc(value),N=value=>UI.number(value);
  function clips(task){
    const values=Array.isArray(task?.clips)?task.clips:[];
    return values.filter(item=>item&&typeof item==='object').map((item,index)=>({...item,index:item.index??index+1}));
  }
  function clipsHTML(task){
    return clips(task).map(item=>{
      const url=item.status==='completed'?UI.safeURL(item.video_url||''):'';
      const dimensions=Number(item.width)>0&&Number(item.height)>0?`<span>${E(item.width)} × ${E(item.height)}</span>`:'';
      return `<article class="clip-result"><div class="result-meta"><strong>视频 ${E(item.index)}</strong>${dimensions}<span>${Number(item.actual_duration)>0?E(item.actual_duration)+' 秒':'时长待核对'}</span>${url?`<a class="btn" href="${E(url)}" target="_blank" rel="noopener" download>下载无水印视频</a>`:''}</div>${url?`<video class="result-video" controls preload="metadata" src="${E(url)}"></video>`:`<p class="hint">${E(item.error||item.message||(item.status==='failed'?'此条结果暂不可下载':'继续等待此条视频'))}</p>`}</article>`;
    }).join('');
  }
  function hasVideo(task){return clips(task).some(item=>item.status==='completed'&&UI.safeURL(item.video_url||''))||!!UI.safeURL(task?.video_url||'');}
  const pendingDownloads=new Set();
  function downloadPath(value){
    if(typeof value!=='string'||!value.trim())return '';
    try{
      const url=new URL(value,location.origin),match=/^\/videos\/([^/]+)$/.exec(url.pathname);
      if(!/^https?:$/.test(url.protocol)||!match)return '';
      const name=decodeURIComponent(match[1]);
      if(!name||name==='.'||name==='..'||/[\\/\x00-\x1f\x7f]/.test(name))return '';
      // Published URLs can use PUBLIC_BASE; download the stored file from this server.
      return '/videos/'+encodeURIComponent(name);
    }catch{return '';}
  }
  function downloadButtons(task){
    if(!task||task.deleted_at)return '';
    const results=clips(task),seen=new Set();
    const files=results.filter(item=>item.status==='completed').map(item=>({...item,path:downloadPath(item.video_url)})).filter(item=>item.path&&!seen.has(item.path)&&seen.add(item.path));
    if(!files.length){const path=downloadPath(task.video_url);if(path)files.push({path,index:1});}
    const separate=results.length>1;
    return files.map(item=>{
      const label=separate?`下载视频 ${item.index}`:'一键下载',busy=pendingDownloads.has(item.path);
      const stem=String(task.id||'video')+(separate?'_'+item.index:'');
      const extension=/\.[a-z0-9]{1,8}$/i.exec(decodeURIComponent(item.path))?.[0]||'.mp4';
      const filename=stem.replace(/[^a-z0-9_-]/gi,'_').slice(0,180)+extension;
      return ` <button type="button" class="btn" data-task-file-download="${E(item.path)}" data-task-file-name="${E(filename)}" data-task-file-label="${E(label)}"${busy?' disabled aria-busy="true"':''}>${E(busy?'下载中…':label)}</button>`;
    }).join('');
  }
  function syncDownloadButtons(path){
    const busy=pendingDownloads.has(path);
    document.querySelectorAll('[data-task-file-download]').forEach(button=>{
      if(button.dataset.taskFileDownload!==path)return;
      button.disabled=busy;button.setAttribute('aria-busy',String(busy));
      button.textContent=busy?'下载中…':button.dataset.taskFileLabel;
    });
  }
  async function downloadClick(event){
    const button=event.target.closest('[data-task-file-download]');
    if(!button)return;
    event.preventDefault();
    const path=downloadPath(button.dataset.taskFileDownload);
    if(!path||button.disabled||pendingDownloads.has(path))return;
    pendingDownloads.add(path);syncDownloadButtons(path);
    let objectURL='';
    try{
      const response=await fetch(path,{credentials:'same-origin',redirect:'error'});
      if(!response.ok)throw new Error(response.status===404?'视频文件不存在，请刷新任务列表后重试':`下载失败（${response.status}），请稍后重试`);
      const file=await response.blob();
      if(!file.size)throw new Error('视频文件为空，请稍后重试');
      if(/^(text\/|application\/(?:json|.*\+json))/i.test(file.type))throw new Error('服务器未返回视频文件，请刷新页面后重试');
      objectURL=URL.createObjectURL(file);
      const link=document.createElement('a');link.href=objectURL;link.download=button.dataset.taskFileName||'video.mp4';link.hidden=true;
      document.body.append(link);link.click();link.remove();
      UI.toast('已开始下载视频');
    }catch(error){UI.toast(error instanceof TypeError?'下载失败，请检查网络后重试':error.message||'下载失败，请稍后重试',true);}
    finally{
      if(objectURL)setTimeout(()=>URL.revokeObjectURL(objectURL),60000);
      pendingDownloads.delete(path);syncDownloadButtons(path);
    }
  }
  function deletable(task){return !!task&&['completed','failed'].includes(task.status)&&!task.deleted_at&&!task.result_check_active&&!task.active_processing&&!task.recovering;}
  function deleteButton(task,role){return deletable(task)?` <button class="btn" data-${role}-delete="${E(task.id)}" title="从列表删除，不返还已用额度">删除</button>`:'';}
  function batchHTML(batch,role='task'){
    if(!batch?.batch_id)return '';
    const counts=batch.counts||batch.progress||{};
    const items=Array.isArray(batch.tasks)?batch.tasks.map((task,index)=>`<li>${task.deleted_at?`第${E(task.batch_index??index+1)}次 · 已删除`:`第${E(task.batch_index??index+1)}次 · ${E({queued:'等待发送',processing:'处理中',completed:'已完成',failed:'失败'}[task.status]||task.status||'待确认')} <button class="btn" data-${role==='member'?'member-task':'task-detail'}="${E(task.id)}">查看任务</button>`}</li>`).join(''):'';
    return `<div class="batch-progress"><strong>批次 ${E(String(batch.batch_id).replace('batch_','').slice(0,12))}</strong><p>计划申请 ${N(batch.requested_count??counts.total)} 次 · 已发送 ${N(counts.submitted??counts.sent??0)} · 已建会话 ${N(counts.conversations??0)} · 完成 ${N(counts.completed??0)} · 失败 ${N(counts.failed??0)} · 待发送 ${N(counts.pending??counts.queued??0)} · 已取得视频 ${N(counts.videos??counts.clips??0)} 条</p><small>会话创建代表提交进度，视频结果会分别更新。</small>${items?`<details><summary>查看各次申请</summary><ol>${items}</ol></details>`:''}${batch.status==='paused'&&Number(counts.pending??counts.queued)>0?`<p>${E(batch.pause_reason||batch.reason||'批次已暂停，已发送任务仍在收取结果。')}</p><button class="btn" data-${role}-batch-resume="${E(batch.batch_id)}">继续未发送申请</button>`:''}</div>`;
  }
  function submissionTasks(response){return Array.isArray(response?.tasks)?response.tasks:response?.id?[response]:[];}
  // Fixed backend quota errors only; a subscription or network block is not exhaustion.
  const exhaustedQuotaPrefixes=[
    'Dola 返回无法生成视频（每日免费视频额度已用完），未重新提交：',
    '账号每日生成上限:',
    'Dola 回复当日视频额度已用完',
    'Dola 专家 Pro 免费 7 天额度已用完，本次未进入视频生成：'
  ];
  function quotaHTML(pools,compact=false){
    if(!pools||typeof pools!=='object')return '';
    const values=Array.isArray(pools)?pools.map(value=>[value.type||value.id,value]):Object.entries(pools);
    return values.filter(([,value])=>value&&typeof value==='object').map(([key,value])=>{
      const label=value.label||(key==='ordinary_fast10'?'普通Fast 10秒':key==='expert_fast'?'专家Fast':'额度');
      const exhausted=value.blocked===true&&exhaustedQuotaPrefixes.some(prefix=>String(value.reason||'').startsWith(prefix));
      const period=key==='ordinary_fast10'?'次/日 · 北京时间凌晨01:00重置':key==='expert_fast'?'次/7天 · 每次使用满7天恢复1次':value.period==='day'?'次/日':'次/周';
      const detail=(exhausted?'平台已确认额度用完 · ':'')+`已用 ${N(value.used??0)} · 预占 ${N(value.reserved??0)} · 待核对 ${N(value.uncertain??0)} · 剩余 ${N(exhausted?0:value.remaining)} / ${N(value.limit)} ${period}${value.reset_at||value.next_available_at?` · ${value.next_available_at?'下次可用':'下次恢复'} ${UI.time(value.next_available_at||value.reset_at)}`:''}`;
      if(compact)return `<div class="tiny nowrap" title="${E(label+'：'+detail)}">${E(key==='ordinary_fast10'?'Fast':key==='expert_fast'?'专家':label)} ${N(exhausted?value.limit:(value.used??0))}/${N(value.limit)}</div>`;
      return `<div class="tiny"><strong>${E(label)}</strong>：${E(detail)}</div>`;
    }).join('');
  }
  let deleteUndoNotice=null;
  function undoNotice(restore){
    if(!deleteUndoNotice){
      const state={entries:[],timer:null,restoring:false};
      const node=document.createElement('div');node.className='toast task-delete-undo';node.setAttribute('role','status');
      const text=document.createElement('span');text.className='task-delete-message';
      const button=document.createElement('button');button.className='btn';button.type='button';
      const dismiss=document.createElement('button');dismiss.className='btn icon-button task-delete-dismiss';dismiss.type='button';dismiss.setAttribute('aria-label','关闭删除提示');dismiss.title='关闭删除提示';dismiss.innerHTML=UI.icon('close');
      const close=()=>{clearTimeout(state.timer);node.remove();if(deleteUndoNotice===state)deleteUndoNotice=null;};
      state.update=()=>{
        if(!state.entries.length){close();return;}
        text.textContent=state.entries.length===1?'已从列表删除，已用额度保留。':`已删除 ${state.entries.length} 条任务，已用额度保留。`;
        button.textContent=state.restoring?'正在撤销…':state.entries.length===1?'撤销':'撤销全部';
        button.disabled=dismiss.disabled=state.restoring;
        clearTimeout(state.timer);
        if(!state.restoring)state.timer=setTimeout(close,60000);
      };
      button.addEventListener('click',async()=>{
        if(state.restoring)return;
        state.restoring=true;
        const entries=[...state.entries];state.update();
        let firstError=null;
        for(const entry of entries){
          try{await entry.restore();state.entries.splice(state.entries.indexOf(entry),1);}
          catch(error){firstError=firstError||error;}
        }
        state.restoring=false;state.update();
        if(firstError)UI.toast(UI.detailError(firstError.message),true);
      });
      dismiss.addEventListener('click',close);
      node.append(text,button,dismiss);
      let stack=document.getElementById('toastStack');
      if(!stack){stack=document.createElement('div');stack.id='toastStack';stack.className='toast-stack';stack.setAttribute('aria-live','polite');document.body.append(stack);}
      stack.append(node);deleteUndoNotice=state;
    }
    deleteUndoNotice.entries.push({restore});deleteUndoNotice.update();
  }
  return {clips,clipsHTML,hasVideo,downloadButtons,downloadClick,deletable,deleteButton,batchHTML,submissionTasks,quotaHTML,undoNotice};
})();
