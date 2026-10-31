/* Platform selection is explicit. Dola legacy defaults remain Dola-only. */
window.PlatformUI=(()=>{
  const names={dola:'Dola',doubao:'豆包'};
  const legacy={platform:'dola',enabled:true,models:[{id:'seedance-2.0',label:'Fast 2.0',enabled:true,durations:[5,10,15],ratios:['default','16:9','9:16','1:1','3:4','4:3']}],references:{images:true,audio:true,video:true}};
  function capabilities(platform,value,profile=''){
    if(!names[platform])throw new Error('未知平台');
    if(profile){
      const selected=platform==='dola'&&(value?.profiles||[]).find(item=>item.id===profile&&item.enabled!==false);
      if(!selected)return {platform,enabled:false,models:[],reason:'当前专家模式尚未开放'};
      const referencePolicy=kind=>{const base=value?.references?.[kind],chosen=selected.references?.[kind];return {...(typeof base==='object'?base:{}),...(typeof chosen==='object'?chosen:{}),enabled:chosen===true||chosen?.enabled===true};};
      return {...value,...selected,platform,enabled:value.enabled===true&&selected.enabled!==false,duration_profile:profile,
        references:{images:referencePolicy('images'),audio:referencePolicy('audio'),video:referencePolicy('video')}};
    }
    if(value?.platform===platform&&typeof value.enabled==='boolean')return value;
    return platform==='dola'?legacy:{platform,enabled:false,reason:'豆包模型、时长和线路尚未完成验证',models:[]};
  }
  function profiles(platform,value){return platform==='dola'?(value?.profiles||[]).filter(item=>item.enabled!==false):[];}
  function models(platform,value,profile=''){
    const caps=capabilities(platform,value,profile);
    return (caps.models||[])
      .filter(m=>m.enabled!==false&&(platform!=='doubao'||!m.entry_mode||['native_controls','ordinary_dialog'].includes(m.entry_mode))&&Array.isArray(m.durations)&&m.durations.length&&(platform!=='dola'||profile||caps.duration_profile||m.id==='seedance-2.0'));
  }
  function modelNotice(platform,model){
    if(platform==='dola'&&model?.duration_profile==='plugin-expert30'&&model?.validation_status==='experimental')return '此专家模型组合尚未实测，实际模型、时长和能否生成以平台结果为准。';
    return platform==='doubao'&&model?.validation_status==='experimental'?'2.5 / 30秒试验：真实申请30秒，仅标准账号；权限和出片尚未实测，平台标注5倍消耗，费用以平台为准。':'';
  }
  function allowed(key,platform){return (key?.allowed_platforms||['dola']).includes(platform);}
  function durationValue(platform,model,value){return Number(value);}
  function durationChoices(platform,model,durations,profile=''){return durations.flatMap(n=>platform==='dola'&&!profile&&model==='seedance-2.0'&&n===15?[]:[{value:String(n),label:n+' 秒'+(model==='seedance-1.0'&&n===15?'（试验）':'')}]);}
  function durationHint(){return '';}
  function batchControl({query,prefix,platform,model,duration,duration_profile='',busy=false}){
    const input=query('#'+prefix+'BatchCount');if(!input)return;
    const allowed=platform==='dola'&&(duration_profile==='plugin-expert30'||model==='seedance-2.0'&&Number(duration)===10);
    input.disabled=busy||!allowed;if(!allowed)input.value='1';
    const note=input.parentElement?.querySelector('small');if(note)note.textContent=allowed?'1～20次申请；确认会话后继续提交，视频在后台分别生成。':'当前组合仅支持单次申请。';
    return allowed;
  }
  function request({platform='dola',model,duration,prompt,ratio,duration_profile='',batch_count=1,resolution,two_part}){if(!names[platform])throw new Error('未知平台');if(typeof batch_count!=='number'||!Number.isInteger(batch_count)||batch_count<1||batch_count>20)throw new Error('申请数量必须是1～20的整数');const body={platform,model,duration:durationValue(platform,model,duration),prompt};if(!Number.isInteger(body.duration)||body.duration<=0)throw new Error('视频时长必须是可用的整数秒');if(ratio&&ratio!=='default')body.ratio=ratio;if(duration_profile){if(platform!=='dola'||duration_profile!=='plugin-expert30')throw new Error('不支持的生成入口');body.duration_profile=duration_profile;}if(two_part!==undefined){if(typeof two_part!=='boolean')throw new Error('两段式选项必须为布尔值');if(two_part&&(duration_profile!=='plugin-expert30'||body.duration!==30))throw new Error('两段式仅支持专家模式30秒');if(duration_profile==='plugin-expert30')body.two_part=two_part;}if(batch_count>1)body.batch_count=batch_count;if(resolution&&resolution!=='auto')body.resolution=resolution;return body;}
  function reads(){let serial=0,platform='';return {start(value){platform=value;return ++serial;},current(ticket,value){return ticket===serial&&platform===value;}};}
  const supports=value=>value===true||value?.enabled===true;
  function imagePolicy(platform,model,caps){
    const value=(model?.references||caps?.references||{}).images,enabled=platform==='dola'&&!caps?.duration_profile||supports(value),localOnly=platform==='doubao'&&value?.local_only===true;
    return {enabled,localOnly,limits:{...(typeof value==='object'?value:{}),enabled},notice:enabled&&platform==='doubao'&&value?.validation_status==='experimental'?'参考图片试验：仅支持本地图片，原生附件就绪后才发送；出片效果待实测。':''};
  }
  function advanced({query,prefix,anchor}){
    if(!query('#'+prefix+'Advanced')){
      const host=document.createElement('div');host.id=prefix+'Advanced';host.hidden=true;
      host.innerHTML=`<div class="field" data-feature="resolution"><label for="${prefix}Resolution">清晰度</label><select id="${prefix}Resolution"><option value="auto">平台默认</option></select></div><div class="field" data-feature="no_watermark"><label><input id="${prefix}NoWatermark" type="checkbox"> 必须取得官方无水印下载</label><small>未取得官方无水印下载时会保留明确状态，不以预览流代替。</small></div><div class="field" data-feature="two_part"><label><input id="${prefix}TwoPart" type="checkbox"> 两段式 · 2×15秒</label><small>申请两条独立原片，分别下载；实际条数与时长以平台结果为准。</small></div>${prefix==='gen'?`<div class="field" data-feature="keep_browser_open"><label><input id="${prefix}KeepBrowserOpen" type="checkbox"> 保留浏览器窗口</label><small>仅单次专家申请可用。查看后关闭窗口，项目会继续收取原会话结果。</small></div>`:''}`;
      query(anchor).insertAdjacentElement('beforebegin',host);
    }
    if(!query('#'+prefix+'AccountCategoryField')){
      const field=document.createElement('div');field.id=prefix+'AccountCategoryField';field.className='field';field.hidden=true;
      field.innerHTML=`<label for="${prefix}AccountCategory">豆包账号类型</label><select id="${prefix}AccountCategory"><option value="普通账号">普通账号</option><option value="标准账号">标准账号</option><option value="">自动选择（普通 / 标准）</option></select><small id="${prefix}AccountCategoryHint"></small>`;
      query('#'+prefix+'Advanced').insertAdjacentElement('beforebegin',field);
    }
    const host=query('#'+prefix+'Advanced'),resolution=query('#'+prefix+'Resolution'),watermark=query('#'+prefix+'NoWatermark'),twoPart=query('#'+prefix+'TwoPart'),keepBrowser=query('#'+prefix+'KeepBrowserOpen'),categoryField=query('#'+prefix+'AccountCategoryField'),category=query('#'+prefix+'AccountCategory'),categoryHint=query('#'+prefix+'AccountCategoryHint');
    const categories=['普通账号','标准账号'];
    let active={},currentPlatform='',expert=false,preferredCategory='普通账号',requiredCategory='';
    function render(platform,model,busy=false,options={}){
      if(currentPlatform==='doubao'&&!requiredCategory&&['',...categories].includes(category.value))preferredCategory=category.value;
      currentPlatform=platform;expert=platform==='dola'&&model?.duration_profile==='plugin-expert30';const features=platform==='doubao'||expert?model?.features||{}:{};
      requiredCategory=platform==='doubao'&&categories.includes(model?.account_category)?model.account_category:'';
      const selectingAccount=platform==='doubao';
      active={resolution:supports(features.resolution),no_watermark:supports(features.no_watermark)};
      if(twoPart)active.two_part=expert&&Number(options.duration)===30;
      if(keepBrowser)active.keep_browser_open=expert&&Number(options.batch_count||1)===1;
      category.value=requiredCategory||preferredCategory;category.disabled=busy||!selectingAccount||!!requiredCategory;categoryField.hidden=!selectingAccount;
      categoryHint.textContent=requiredCategory?'此模型仅使用'+requiredCategory+'。':'只使用选定类型的账号；选择“自动选择”才会混用两类账号。';
      const values=active.resolution?(features.resolution?.values||[]).filter(value=>['480p','720p','1080p'].includes(value)):[],previous=resolution.value;
      resolution.replaceChildren(...['auto',...values].map(value=>{const option=document.createElement('option');option.value=value;option.textContent=value==='auto'?'平台默认':value;return option;}));
      resolution.value=values.includes(previous)?previous:model?.duration_profile==='plugin-expert30'&&values.includes('720p')?'720p':'auto';
      for(const [feature,enabled] of Object.entries(active))host.querySelector('[data-feature="'+feature+'"]').hidden=!enabled;
      if(!active.no_watermark)watermark.checked=false;
      if(twoPart){if(!active.two_part)twoPart.checked=false;twoPart.disabled=busy||!active.two_part;}
      if(keepBrowser){if(!active.keep_browser_open)keepBrowser.checked=false;keepBrowser.disabled=busy||!active.keep_browser_open;}
      host.hidden=!Object.values(active).some(Boolean);resolution.disabled=busy||!active.resolution;watermark.disabled=busy||!active.no_watermark;
    }
    function values(){
      if(currentPlatform!=='doubao'){const body=active.resolution&&resolution.value!=='auto'?{resolution:resolution.value}:{};if(expert&&active.two_part)body.two_part=twoPart.checked;if(expert&&active.keep_browser_open&&keepBrowser.checked)body.keep_browser_open=true;return body;}
      const selectedCategory=requiredCategory||category.value;
      if(!['',...categories].includes(selectedCategory))throw new Error('请选择豆包账号类型');
      const body={account_category:selectedCategory||null};if(active.resolution&&resolution.value!=='auto')body.resolution=resolution.value;if(active.no_watermark&&watermark.checked)body.require_no_watermark=true;
      return body;
    }
    return {render,values};
  }
  const mediaAccept=refs=>[...(supports(refs.audio)?['.mp3','.wav','.m4a','.aac','.flac','.ogg']:[]),...(supports(refs.video)?['.mp4','.mov','.webm']:[])].join(',');
  return {names,capabilities,profiles,models,modelNotice,allowed,durationValue,durationChoices,durationHint,batchControl,request,reads,supports,imagePolicy,advanced,mediaAccept};
})();
