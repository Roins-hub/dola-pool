const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const context={window:{},UI:{esc:value=>String(value).replaceAll('&','&amp;').replaceAll('<','&lt;').replaceAll('>','&gt;'),time:x=>String(x)}};
vm.runInNewContext(fs.readFileSync('web/task-evidence.js','utf8'),context);
const api=context.window.TaskEvidence;
const dailyLimit={platform:'dola',status:'failed',phase:'platform_daily_limit'};
assert.ok(api.badge(dailyLimit).includes('每日免费视频额度已用完'));
assert.ok(api.badge(dailyLimit).includes('b-red'));
assert.ok(!api.badge(dailyLimit).includes('等待'));
assert.ok(api.failureExplanation(dailyLimit).includes('未进入视频生成'));
assert.ok(api.badge({status:'failed',phase:'subscription_required'}).includes('需要会员'));
assert.ok(api.badge({status:'failed',phase:'partial_results'}).includes('部分完成'));
assert.ok(api.badge({status:'queued',phase:'waiting_quota'}).includes('等待对应额度'));
const watermarkSetup=api.badge({status:'failed',phase:'watermark_setup_failed'});
assert.ok(watermarkSetup.includes('无水印设置未通过，未提交'));
assert.ok(watermarkSetup.includes('b-red'));
const reviewBadge=api.badge({platform:'doubao',status:'failed',phase:'platform_review_rejected'});
assert.ok(reviewBadge.includes('生成失败 · 内容／版权审核'));
assert.ok(reviewBadge.includes('b-red'));
assert.ok(!reviewBadge.includes('等待成片'));
assert.ok(!reviewBadge.includes('未计额度'));
assert.ok(api.badge({status:'failed',phase:'text_only_ack'}).includes('仅文字回复，未确认生成'));
assert.ok(api.content({request_evidence:{generation_evidence:{confirmed:false,final_text_only:true}}}).includes('仅发现已结束的普通文字消息'));
assert.ok(api.content({request_evidence:{generation_evidence:{confirmed:true,task_ids:['12345']}}}).includes('平台任务：12345'));
assert.ok(api.content({request_evidence:{generation_evidence:{confirmed:false,final_text_only:true,request_parameters_echoed:true}}}).includes('不代表视频生成已启动'));
assert.ok(!api.content({request_evidence:{generation_evidence:{confirmed:false}}}).includes('旧记录：尚未通过核对'));
for(const [phase,label] of [['checking_verification','检查验证入口'],['waiting_verification','等待人工验证'],
  ['verification_continue','验证后等待原会话回执'],['verification_incomplete','验证未完成']]){
  const task={platform:'doubao',status:phase==='verification_incomplete'?'failed':'processing',phase};
  assert.ok(api.badge(task).includes(label));
  if(task.status==='processing')assert.ok(api.content(task).includes('不要重复点击生成'));
}
assert.ok(api.badge({status:'processing',phase:'awaiting_receipt',conversation_id:'123'}).includes('等待生成回执'));
assert.ok(!api.badge({status:'processing',phase:'awaiting_receipt'}).includes('已受理'));
assert.ok(!api.badge({status:'completed',phase:'queued'}).includes('时长已核对'));
assert.ok(api.badge({status:'failed',phase:'duration_mismatch',actual_duration:15}).includes('时长不符'));
assert.ok(api.content({dola_reply:'<img src=x onerror=alert(1)>',request_evidence:'bad json'}).includes('&lt;img'));
console.log('task evidence: phases and safe replies verified');

const cases=[
  ['Unqualified watermark delivery is an actionable failure, not missing generation',()=>{
    const task={platform:'dola',status:'failed',phase:'no_watermark_unavailable',actual_duration:15,error:'已有成片，来源未核对'};
    assert.ok(api.badge(task).includes('已出片，未取得无水印'));
    assert.ok(api.badge(task).includes('b-red'));
    assert.ok(api.content(task).includes('已有成片，来源未核对'));
  }],
  ['Doubao failed controls show their escaped concrete cause without inventing acceptance',()=>{
    const task={platform:'doubao',status:'failed',phase:'failed_before_submit',error:'图片入口不唯一 <tag>；提示词未发送。'};
    assert.equal(api.failureExplanation(task),task.error);
    assert.ok(api.content(task).includes('图片入口不唯一 &lt;tag&gt;'));
    assert.ok(api.badge(task).includes('b-red'));
    assert.ok(api.badge(task).includes('未发送'));
    assert.equal(api.failureExplanation({...task,status:'processing',phase:'submission_unknown'}),'');
    assert.equal(api.failureExplanation({...task,status:'completed'}),'');
    assert.equal(api.failureExplanation({...task,platform:'dola'}),'');
  }],

  ['Doubao replies and task stages use their own platform identity',()=>{
    const task={platform:'doubao',model:'seedance-2.0',duration:15,duration_profile:'fast15-via30',platform_reply:'豆包已受理 <tag>',dola_reply:'错误平台旧回复',status:'processing',phase:'submission_unknown'};
    assert.equal(api.isFast15Profile(task),false);
    assert.equal(api.durationText(task),'请求 15 秒');
    const html=api.content(task);
    assert.ok(html.includes('豆包 原回复'));
    assert.ok(html.includes('豆包已受理 &lt;tag&gt;'));
    assert.ok(!html.includes('错误平台旧回复'));
    assert.ok(!html.includes('上游时长参数30秒'));
    assert.ok(api.badge(task).includes('受理待确认'));
    assert.ok(api.badge({...task,phase:'waiting_lane'}).includes('等待线路'));
    assert.ok(api.badge({...task,phase:'preparing_page'}).includes('准备页面'));
  }],
  ['explicit no-charge policy rejection is a terminal red failure with local-only quota explanation',()=>{
    for(const task of [{status:'failed',phase:'platform_rejected_no_charge'},
      {status:'failed',phase:'failed',failure_code:'platform_rejected_no_charge'}]){
      assert.ok(api.badge(task).includes('生成失败 · 内容限制（未计额度）'));
      assert.ok(api.badge(task).includes('b-red'));
      assert.ok(api.content(task).includes('平台已拒绝生成，本地本次额度已释放；请修改内容后手动提交。'));
    }
  }],
  ['ordinary refusal copyright and unknown metadata cannot invent local quota release',()=>{
    for(const task of [{status:'failed',phase:'platform_rejected'}, {status:'failed',phase:'platform_copyright'},
      {status:'failed',phase:'platform_audio_copyright'}, {status:'failed',dola_reply:'Policy denied; used 0 credits'}]){
      assert.ok(!api.badge(task).includes('未计额度'));
      assert.ok(!api.content(task).includes('本地本次额度已释放'));
    }
  }],
  ['completed historical video overrides a stale no-charge rejection phase',()=>{
    const task={status:'completed',phase:'platform_rejected_no_charge',actual_duration:15.105,video_url:'/videos/history.mp4'};
    assert.ok(api.badge(task).includes('b-green'));
    assert.ok(!api.badge(task).includes('未计额度'));
    assert.ok(!api.content(task).includes('本地本次额度已释放'));
    assert.ok(api.content(task).includes('已下载，实际 15.105 秒'));
  }],
  ['mapped new15 presents the target separately from the upstream parameter',()=>{
    const task={model:'seedance-2.0',duration:15,duration_profile:'fast15-via30',actual_duration:15.105,
      status:'completed',video_url:'/downloads/new15.mp4',request_evidence:{model:'seedance_v2.0',duration:30,audit_status:'passed'}};
    for(const model of ['seedance-2.0','seedance_v2.0','seedance_20','SEEDANCE-V20']){
      assert.equal(api.durationText({...task,model}),'目标 15 秒 / 实际 15.105 秒');
      assert.ok(api.isFast15Profile({...task,model}));
    }
    const html=api.content(task);
    assert.ok(html.includes('上游时长参数30秒（新15秒链路）'));
    assert.ok(html.includes('目标15秒'));
    assert.ok(!html.includes('请求 30 秒'));
  }],
  ['legacy30 and nonmatching profile records retain their original duration semantics',()=>{
    for(const task of [{model:'seedance-2.0',duration:30},{model:'seedance-2.5',duration:15,duration_profile:'fast15-via30'},
      {model:'seedance-1.0',duration:15,duration_profile:'fast15-via30'},{model:'seedance_v2.0_std',duration:15,duration_profile:'fast15-via30'},
      {model:'seedance-2.0',duration:15,duration_profile:'fast15-via30-extra'},{model:'seedance-2.0',duration:30,duration_profile:'fast15-via30'}]){
      assert.equal(api.durationText(task),'请求 '+task.duration+' 秒');
      assert.ok(!api.content(task).includes('上游时长参数30秒（新15秒链路）'));
    }
  }],
  ['verified video overrides an old request warning',()=>{
    const task={status:'completed',phase:'request_mismatch',actual_duration:30.08,video_url:'/downloads/result.mp4',
      request_evidence:{audit_status:'mismatch',model_matches:true,duration_matches:true,ratio_matches:true,prompt_matches:false}};
    assert.equal(api.phase(task),'completed');
    assert.ok(api.badge(task).includes('b-green'));
    const html=api.content(task);
    assert.ok(html.includes('请求核对'));
    assert.ok(html.includes('生成进度'));
    assert.ok(html.includes('成片结果'));
    assert.ok(html.includes('30.08 秒'));
    assert.ok(html.includes('历史核对记录'));
    assert.ok(!api.badge(task).includes('核对失败'));
  }],
  ['format differences are distinct from exact prompt matches',()=>{
    const base={status:'processing',phase:'accepted',request_evidence:{audit_status:'format_equivalent',model:'seedance_v2.5',duration:30,ratio:'16:9',
      model_matches:true,duration_matches:true,ratio_matches:true,prompt_matches:true,prompt_exact:false,reason:'仅平台标签、画幅和末尾空格差异'}};
    const equivalent=api.content(base);
    assert.ok(equivalent.includes('平台格式等价'));
    assert.ok(equivalent.includes('提示词：平台格式等价'));
    assert.ok(equivalent.includes('仅平台标签、画幅和末尾空格差异'));
    const exact=api.content({...base,request_evidence:{...base.request_evidence,audit_status:'passed',prompt_exact:true}});
    assert.ok(exact.includes('提示词：原文一致'));
  }],
  ['unknown audit fields do not override generation acceptance',()=>{
    const task={status:'processing',phase:'accepted',request_evidence:{audit_status:'unknown',model_matches:true,duration_matches:null,ratio_matches:null,prompt_matches:null}};
    assert.ok(api.badge(task).includes('已受理'));
    const html=api.content(task);
    assert.ok(html.includes('无法完整核对'));
    assert.ok(html.includes('模型：核对通过'));
    assert.ok(html.includes('时长：尚未读取'));
    assert.ok(html.includes('提示词：尚未读取'));
    assert.ok(!html.includes('提示词：内容不符'));
  }],
  ['mismatch details identify only the failing fields',()=>{
    const html=api.content({status:'processing',phase:'polling',request_evidence:{audit_status:'mismatch',model_matches:false,duration_matches:true,ratio_matches:true,prompt_matches:false,prompt_exact:false}});
    assert.ok(html.includes('模型：不符'));
    assert.ok(html.includes('时长：核对通过'));
    assert.ok(html.includes('提示词：内容不符'));
    assert.ok(html.includes('已受理，后台查询'));
  }],
  ['platform failure types remain separate from query timeout',()=>{
    for(const [code,label] of [['platform_copyright','平台版权限制'],['platform_audio_copyright','平台音频版权限制'],['platform_rejected','平台拒绝生成']]){
      const html=api.badge({status:'failed',phase:'failed',failure_code:code});
      assert.ok(html.includes(label),code);
      assert.ok(html.includes('b-red'),code);
    }
    const timeout=api.badge({status:'failed',phase:'query_timeout'});
    assert.ok(timeout.includes('查询超时'));
    assert.ok(!timeout.includes('b-red'));
  }],
  ['actual duration mismatch keeps its available result',()=>{
    const html=api.content({status:'failed',phase:'duration_mismatch',duration:30,actual_duration:15.08,video_url:'/downloads/short.mp4'});
    assert.ok(html.includes('15.08 秒'));
    assert.ok(html.includes('时长不符'));
    assert.ok(!html.includes('时长已核对'));
  }],
  ['legacy evidence and missing evidence are not fabricated',()=>{
    assert.ok(api.content({request_evidence:{matches:true,model:'seedance_v2.5',duration:30}}).includes('旧记录：核对通过'));
    assert.ok(api.content({request_evidence:{matches:false}}).includes('具体差异未记录'));
    assert.ok(api.content({request_evidence:'bad json'}).includes('尚未取得提交核对记录'));
    assert.ok(api.badge({status:'completed',phase:'request_mismatch',actual_duration:null}).includes('旧记录未核对实际时长'));
  }],
  ['audit reasons and every reply are safely rendered',()=>{
    const unsafe='<script>alert(1)</script>';
    const html=api.content({request_evidence:{audit_status:'mismatch',reason:unsafe},dola_reply:unsafe},[{phase:'platform_copyright',at:'now',details:{reason:unsafe}}]);
    assert.ok(!html.includes('<script>'));
    assert.equal((html.match(/&lt;script&gt;/g)||[]).length,3);
    assert.ok(html.includes('平台版权限制'));
  }]
];
let failures=0;
for(const [name,run] of cases){
  try {run();console.log('PASS '+name);}
  catch(error){failures++;console.error('FAIL '+name+': '+error.message);}
}
if(failures)process.exitCode=1;

(async()=>{
  let resolve,opens=0;
  const drawer={hidden:true,dataset:{}};
  const page={window:{addEventListener(){}},document:{addEventListener(){}},
    q:selector=>drawer,api:()=>new Promise(done=>{resolve=done;}),TaskEvidence:api,
    UI:{...context.UI,number:String,icon:()=>'',safeURL:()=>'',detailError:String,
      openDrawer(){opens++;drawer.hidden=false;delete drawer.dataset.requestKey;return drawer;}}};
  vm.runInNewContext(fs.readFileSync('web/admin-ui.js','utf8'),page);
  const loading=page.window.AdminUI.taskDetail('video_fixture');
  drawer.hidden=true;
  resolve({task:{id:'video_fixture',status:'processing',model:'seedance-2.5'},events:[]});
  await loading;
  assert.equal(opens,1,'A slow detail response must not reopen the drawer after the user closes it');
  assert.equal(drawer.hidden,true);
  console.log('task evidence: closing details survives a delayed response');
})().catch(error=>{console.error(error);process.exitCode=1;});
assert.equal(api.durationText({platform:'dola',duration:15,duration_profile:'pro-fast15'}),'专家模式 请求 15 秒');
assert.equal(api.durationText({platform:'dola',duration:15,duration_profile:'plugin-expert30'}),'专家申请15秒 / 等待成片');
assert.equal(api.durationText({platform:'dola',duration:5,duration_profile:'plugin-expert30',clips:[{status:'completed',actual_duration:5.1}],total_actual_duration:5.1}),'专家申请5秒 / 1条：5.1秒，合计5.1秒');
