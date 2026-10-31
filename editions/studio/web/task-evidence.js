/* Shared task presentation. A conversation ID never means accepted generation. */
window.TaskEvidence = (() => {
  const labels = {queued:'等待调度',account_retry:'正在换号重试',preparing:'准备浏览器',trace_ready:'专家链路记录已开启',sending:'正在发送',submitted:'已发送，核对请求',
    awaiting_receipt:'等待生成回执',accepted:'已受理，等待成片',polling:'已受理，后台查询',
    waiting_lane:'等待线路',preparing_page:'准备页面',submitting:'正在提交',submission_unknown:'受理待确认',media_ready:'已有成片，等待下载',
    downloading:'已有成片，下载中',completed:'已完成，时长已核对',not_accepted:'未确认受理',failed_before_submit:'准备失败，提示词未发送',
    request_mismatch:'提交参数核对失败',download_failed:'已有成片，下载失败',duration_mismatch:'已出片，时长不符',
    no_watermark_unavailable:'已出片，未取得无水印',watermark_setup_failed:'无水印设置未通过，未提交',download_metadata:'核对成片下载来源',
    platform_copyright:'平台版权限制',platform_audio_copyright:'平台音频版权限制',platform_rejected:'平台拒绝生成',
    platform_rejected_no_charge:'生成失败 · 内容限制（未计额度）',
    platform_review_rejected:'生成失败 · 内容／版权审核',expert_weekly_limit:'专家 Pro 本周额度已用完',
    platform_daily_limit:'每日免费视频额度已用完',
    partial_results:'部分完成，可下载已有视频',subscription_required:'此生成能力需要会员',
    waiting_quota:'等待对应额度',batch_paused:'批量申请已暂停',
    checking_verification:'检查验证入口',waiting_verification:'等待人工验证',
    verification_continue:'验证后等待原会话回执',verification_incomplete:'验证未完成',text_only_ack:'仅文字回复，未确认生成',
    query_timeout:'查询超时，尚未确认结果',failed:'失败'};
  function phase(task) {
    if(task.deleted_at)return 'deleted';
    if(task.status==='completed'&&Array.isArray(task.clips)&&task.clips.length&&task.clips.every(c=>c.status==='completed'&&Number(c.actual_duration)>0))return 'completed';
    if (task.status === 'completed') return Number.isFinite(Number(task.actual_duration)) && Number(task.actual_duration)>0 ? 'completed' : 'legacy_completed';
    if (task.status === 'failed' && (!task.phase || task.phase === 'failed') && labels[task.failure_code]) return task.failure_code;
    if (task.phase && !(task.phase === 'queued' && task.status !== 'queued')) return task.phase;
    return task.status === 'processing' ? 'legacy_processing' : task.status;
  }
  function badge(task) {
    const p=phase(task), label=labels[p] || (p==='legacy_processing'?'处理中，旧记录缺少回执证据':p==='legacy_completed'?'已完成，旧记录未核对实际时长':p || '未知');
    const color=['failed','failed_before_submit','not_accepted','request_mismatch','download_failed','duration_mismatch','no_watermark_unavailable','watermark_setup_failed','platform_copyright','platform_audio_copyright','platform_rejected','platform_rejected_no_charge','platform_review_rejected','expert_weekly_limit','platform_daily_limit','verification_incomplete','text_only_ack','subscription_required'].includes(p)?'b-red':
      ['completed','legacy_completed'].includes(p)?'b-green':['queued','account_retry','preparing','trace_ready','sending','submitted','awaiting_receipt','query_timeout','waiting_lane','preparing_page','submitting','submission_unknown','checking_verification','waiting_verification','verification_continue'].includes(p)?'b-orange':'b-blue';
    return `<span class="badge ${color}"${verificationHint(task)?` title="${UI.esc(verificationHint(task))}"`:''}>${UI.esc(label)}</span>`;
  }
  function isFast15Profile(task) {
    const model=String(task.model||'').trim().toLowerCase().replaceAll('-','_');
    return (task.platform||'dola')==='dola'&&task.duration_profile==='fast15-via30'&&Number(task.duration)===15&&
      ['seedance_2.0','seedance_v2.0','seedance_20','seedance_v20'].includes(model);
  }
  function durationText(task) {
    if(task.duration_profile==='plugin-expert30'){
      const clips=Array.isArray(task.clips)?task.clips:[],ready=clips.filter(c=>c.status==='completed'&&Number(c.actual_duration)>0);
      return '专家申请'+(Number(task.duration)>0?Number(task.duration):30)+'秒'+(ready.length?' / '+ready.length+'条：'+ready.map(c=>Number(c.actual_duration)+'秒').join(' + '):' / 等待成片')+(Number(task.total_actual_duration)>0?'，合计'+Number(task.total_actual_duration)+'秒':'');
    }
    const actual=Number(task.actual_duration),duration=task.duration??'未知';
    const expert=(task.platform||'dola')==='dola'&&task.duration_profile==='pro-fast15';
    return `${expert?'专家模式 请求':isFast15Profile(task)?'目标':'请求'} ${duration} 秒${Number.isFinite(actual)&&actual>0?' / 实际 '+actual+' 秒':''}`;
  }
  function failureExplanation(task) {
    if(phase(task)==='platform_daily_limit')return 'Dola 已明确回复每日免费视频额度已用完，本次未进入视频生成；已结束等待，保留原会话。';
    if(phase(task)==='subscription_required')return '平台提示此能力需要会员；本次已停止，后续申请不会自动切换生成模式。';
    if(phase(task)==='partial_results')return task.error||'已取得的合格视频可以分别下载，剩余结果可从原会话补收。';
    if(phase(task)==='expert_weekly_limit')return task.error || 'Dola 专家 Pro 的免费 7 天额度已用完；本次未进入视频生成。';
    if(phase(task)==='watermark_setup_failed')return task.error || '账号无水印设置未确认，提示词未发送。';
    if(phase(task)==='no_watermark_unavailable')return task.error || '已有成片，但未确认官方无水印版本；保留原会话供补收，未重新生成。';
    if(phase(task)==='platform_rejected_no_charge')return '平台已拒绝生成，本地本次额度已释放；请修改内容后手动提交。';
    return task.platform==='doubao'&&task.status==='failed'&&typeof task.error==='string'?task.error:'';
  }
  function verificationHint(task) {
    const p=phase(task);
    if(task.platform!=='doubao')return '';
    if(p==='checking_verification')return '正在等待豆包官方验证入口加载，尚未确认受理。不要重复点击生成。';
    if(p==='waiting_verification')return '请在运行项目的电脑上，在保留的豆包窗口内手动完成验证。最多等待15分钟；不要重复点击生成。';
    if(p==='verification_continue')return '验证弹窗已消失，正在等待原会话的真实回执。若官网仍要求继续，请在原窗口处理；程序不会自动补发提示词。不要重复点击生成。';
    return '';
  }
  function content(task, events=[]) {
    const E=UI.esc;
    let request;
    try { request=typeof task.request_evidence==='string'?JSON.parse(task.request_evidence):task.request_evidence; } catch {}
    const execution=request?.generation_evidence;
    if(request && Object.keys(request).every(key=>key==='generation_evidence'))request=null;
    const auditLabels={passed:'完整请求核对通过',format_equivalent:'平台格式等价，正文核对通过',unknown:'无法完整核对',mismatch:'请求存在差异'};
    const audit=request?(auditLabels[request.audit_status] || (request.matches===true?'旧记录：核对通过':'旧记录：尚未通过核对，具体差异未记录')):'尚未取得提交核对记录';
    const checks=request?[
      ['模型',request.model_matches],['时长',request.duration_matches],['画幅',request.ratio_matches],['提示词',request.prompt_matches]
    ].map(([name,value])=>{
      let label=value===true?'核对通过':value===false?'不符':'尚未读取';
      if(name==='提示词' && value===false)label='内容不符';
      if(name==='提示词' && value===true)label=request.prompt_exact===true?'原文一致':request.prompt_exact===false?'平台格式等价':'核对通过';
      return `<li>${E(name+'：'+label)}</li>`;
    }).join(''):'';
    const explanation=request?.reason || request?.audit_reason;
    const mapped=isFast15Profile(task);
    const evidence=`<div class="detail-block"><h3>请求核对</h3>${mapped?'<p class="hint">目标15秒；上游时长参数30秒（新15秒链路）</p>':''}${request?`<p>${E(request.model || '未读取')} · ${mapped?'上游参数 ':''}${E(request.duration ?? '未知')} 秒 · ${E(request.ratio || '未读取画幅')}</p>`:''}<p>${E(audit)}</p>${checks?`<ul>${checks}</ul>`:''}${explanation?`<p>${E(explanation)}</p>`:''}${phase(task)==='completed' && request && !['passed','format_equivalent'].includes(request.audit_status)?'<p class="hint">以上为历史核对记录，当前成片结果已确认。</p>':''}</div>`;
    const progress=`<div class="detail-block"><h3>生成进度</h3>${badge(task)}${verificationHint(task)?`<p class="hint">${E(verificationHint(task))}</p>`:''}</div>`;
    const failure=failureExplanation(task)?`<div class="detail-block"><h3>失败说明</h3><p class="error-text">${E(failureExplanation(task))}</p></div>`:'';
    const actual=Number(task.actual_duration),hasDuration=Number.isFinite(actual) && actual>0;
    let result=task.video_url?(hasDuration?`${task.status==='completed'?'已下载':'已有视频'}，实际 ${actual} 秒`:'已有视频，实际时长尚未核对'):'尚无可下载视频';
    if(phase(task)==='duration_mismatch')result+=`；${mapped?'目标':'请求'} ${task.duration ?? '未知'} 秒，时长不符`;
    const video=`<div class="detail-block"><h3>成片结果</h3><p>${E(result)}</p></div>`;
    const platform=task.platform||'dola',platformName=platform==='doubao'?'豆包':platform==='dola'?'Dola':'未知平台';
    const reply=`<div class="detail-block"><h3>${platformName} 原回复</h3><pre style="white-space:pre-wrap;overflow-wrap:anywhere;font:inherit">${E((platform==='dola'?(task.dola_reply||task.platform_reply):task.platform_reply) || '尚未记录回复；可查询已保存的原会话。')}</pre></div>`;
    const history=events.length?`<div class="detail-block"><h3>任务过程</h3><ol>${events.map(event=>`<li>${E(UI.time(event.at))} · ${E(labels[event.phase] || event.phase)}${event.details?.reason?`<p>${E(event.details.reason)}</p>`:''}${event.phase==='account_retry'?`<p>${E('账号：'+(event.details?.account || '未知'))} · ${E('原会话：'+(event.details?.conversation_id || '未取得'))}</p>${event.details?.reply?`<p>${E(event.details.reply)}</p>`:''}`:''}</li>`).join('')}</ol></div>`:'';
    const echo=execution?.request_parameters_echoed&&!execution.confirmed?'<p>平台已回显模型、时长等参数，但这不代表视频生成已启动。</p>':'';
    const generation=execution?`<div class="detail-block"><h3>实际生成核对</h3><p>${E(execution.confirmed?'已发现平台原生视频调用或生成记录':execution.final_text_only?'仅发现已结束的普通文字消息，未确认视频生成':'尚无可核对的平台生成记录')}</p>${echo}${Array.isArray(execution.task_ids)&&execution.task_ids.length?`<p>平台任务：${execution.task_ids.map(E).join('、')}</p>`:''}${execution.parameters_match===false?'<p class="error-text">平台调用参数与本次请求不一致。</p>':''}</div>`:'';
    return evidence+generation+progress+failure+video+reply+history;
  }
  return {badge,content,phase,isFast15Profile,durationText,failureExplanation,verificationHint};
})();
