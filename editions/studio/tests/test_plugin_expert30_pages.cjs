/* Real pages with all HTTP fulfilled locally. No account or live generation. */
const {test}=require('node:test');
const assert=require('node:assert/strict');
const fs=require('node:fs');
const path=require('node:path');
const {chromium}=require('./browser-runtime.cjs');
const models=['seedance-2.5','seedance-2.0','seedance-2.0-std','seedance-2.0-mini'].map(id=>({id,label:id,enabled:true,durations:[5,10,15,30],ratios:['1:1','3:4','4:3','9:16','16:9','21:9']}));
const caps={platform:'dola',enabled:true,models:[{id:'seedance-2.0',label:'Fast',durations:[5,10,15],ratios:['default','16:9']}],references:{images:{enabled:true,max_count:3},audio:{enabled:true,max_count:1},video:{enabled:true,max_count:1}},profiles:[{id:'plugin-expert30',label:'专家模式',enabled:true,default_model:'seedance-2.5',default_duration:30,models,references:{images:true,audio:true,video:true}}]};
async function fixture(role){
  const browser=await chromium.launch({headless:true});const context=await browser.newContext();
  await context.addInitScript(()=>{localStorage.setItem('dola_token','fixture');window.setInterval=()=>0;});
  const records=[{id:'video_dual',platform:'dola',prompt:'fixture',status:'failed',duration:30,duration_profile:'plugin-expert30',clips:[{clip_id:'one',index:1,status:'completed',actual_duration:15,video_url:'https://fixture.test/videos/first.mp4'},{clip_id:'two',index:2,status:'failed',error:'等待无水印'}]}];
  const requests=[],errors=[];let hidden=false;
  await context.route('**/*',async route=>{
    const req=route.request(),u=new URL(req.url()),p=u.pathname;let body;try{body=req.postDataJSON();}catch{}
    requests.push({path:p,method:req.method(),body});
    const json=data=>route.fulfill({contentType:'application/json',body:JSON.stringify(data)});
    if(p.endsWith('/video-capabilities'))return json(caps);
    if(p==='/api/me')return json({user:{email:'fixture@test',role:'member'},keys:[{key:'fixture-key',name:'fixture',enabled:true,allowed_platforms:['dola'],allowed_durations:[5,10,15,30],usage:{}}],usage:{},supported_durations:[5,10,15,30]});
    if(p.endsWith('/tasks/video_dual/restore')){hidden=false;return json({ok:true,task:records[0]});}
    if(p.endsWith('/tasks/video_dual')){if(req.method()==='DELETE'){hidden=true;return json({ok:true});}return json({task:records[0],events:[]});}
    if(p.endsWith('/tasks'))return json({tasks:hidden?[]:records});
    if(p.includes('/batches/'))return json({batch:{batch_id:'batch_fixture',requested_count:2,counts:{sent:2,conversations:2,completed:0,failed:0,pending:0,videos:1}}});
    if(p.endsWith('/config'))return json({max_count:3,max_bytes:15728640});
    if(req.method()==='POST'&&p.endsWith('/reference-media'))return json({id:'fixture-media'});
    if(req.method()==='POST'&&p.endsWith('/reference-images'))return json({id:'fixture-image'});
    if(p.endsWith('/generate')||p.endsWith('/generations'))return json({batch_id:'batch_fixture',requested_count:2,tasks:[{id:'video_a',status:'queued'},{id:'video_b',status:'queued'}],counts:{sent:0,pending:2}});
    if(p.startsWith('/api/')||p.startsWith('/v1/'))return json({});
    const name=p==='/admin'?'index.html':p==='/member'?'member.html':p.slice(1),file=path.join(__dirname,'../web',name);
    if(!fs.existsSync(file))return route.fulfill({status:404,body:''});
    return route.fulfill({contentType:name.endsWith('.js')?'text/javascript':name.endsWith('.css')?'text/css':'text/html',body:fs.readFileSync(file)});
  });
  const page=await context.newPage();page.setDefaultTimeout(5000);page.on('pageerror',error=>errors.push(String(error)));
  await page.goto('https://fixture.test/'+role+(role==='admin'?'#gallery':''));
  return {browser,page,requests,errors};
}
test('both pages submit expert modes with independent batch count and preserve all parameters',async()=>{
  for(const role of ['admin','member']){
    const {browser,page,requests,errors}=await fixture(role);const p=role==='admin'?'gen':'member';
    try{
      await page.locator('#'+p+'Profile option[value="plugin-expert30"]').waitFor({state:'attached'});
      await page.locator('#'+p+'Profile').selectOption('plugin-expert30');
      assert.equal(await page.locator('#'+p+'Model').inputValue(),'seedance-2.5');
      assert.equal(await page.locator('#'+p+'Duration').inputValue(),'30');
      assert.equal(await page.locator('#'+p+'Ratio').inputValue(),'16:9');
      assert.equal(await page.locator('#'+p+'Resolution').inputValue(),'720p');
      assert.equal(await page.locator('#'+p+'MediaFiles').isVisible(),true);
      assert.equal(await page.locator('#'+p+'TwoPart').isChecked(),false);
      if(role==='member'){
        await page.locator('#memberKey').dispatchEvent('change');
        await page.waitForTimeout(100);
        assert.equal(await page.locator('#memberProfile').inputValue(),'plugin-expert30','capability refresh keeps the selected generation route');
      }
      await page.locator('#'+p+'Model').selectOption('seedance-2.0-mini');
      await page.locator('#'+p+'Ratio').selectOption('21:9');
      await page.locator('#'+p+'Resolution').selectOption('480p');
      await page.locator('#'+p+'BatchCount').fill('2');
      await page.locator('#'+p+'TwoPart').check();
      await page.locator('#'+p+'Prompt').fill('保留原文');
      await page.locator(role==='admin'?'#genBtn':'#memberSubmit').click();
      await page.locator('#'+p+'Status').filter({hasText:'批次'}).waitFor();
      const request=requests.findLast(r=>r.path===(role==='admin'?'/api/admin/generate':'/v1/videos/generations'));
      assert.equal(request.body.duration_profile,'plugin-expert30');assert.equal(request.body.model,'seedance-2.0-mini');
      assert.equal(request.body.batch_count,2);assert.equal(request.body.duration,30);assert.equal(request.body.ratio,'21:9');assert.equal(request.body.resolution,'480p');
      assert.equal(request.body.two_part,true);assert.equal(request.body.keep_browser_open,undefined);
      assert.deepEqual(errors,[]);
    }finally{await browser.close();}
  }
});
test('both pages expose partial clips and deletion with undo without submitting generation',async()=>{
  for(const role of ['admin','member']){
    const {browser,page,requests,errors}=await fixture(role);
    try{
      await page.locator(role==='admin'?'#adminResult':'#memberResult').getByText('等待无水印').waitFor();
      assert.equal(await page.locator(role==='admin'?'#adminResult video':'#memberResult video').count(),1);
      const selector=role==='admin'?'[data-task-delete="video_dual"]':'[data-member-delete="video_dual"]';
      await page.locator(selector).first().click();
      await page.locator('.task-delete-undo').waitFor();
      await page.locator('.task-delete-undo').getByRole('button',{name:'撤销',exact:true}).click();
      assert.equal(requests.filter(r=>r.method==='DELETE').length,1);
      assert.equal(requests.filter(r=>r.method==='POST'&&r.path.endsWith('/restore')).length,1);
      assert.equal(requests.filter(r=>r.path.endsWith('/generate')||r.path.endsWith('/generations')).length,0);
      assert.deepEqual(errors,[]);
    }finally{await browser.close();}
  }
});
test('switching to expert preserves and uploads an already selected video reference',async()=>{
  for(const role of ['admin','member']){
    const {browser,page,requests}=await fixture(role);const p=role==='admin'?'gen':'member';
    try{
      await page.locator('#'+p+'MediaFiles').setInputFiles({name:'reference.mp4',mimeType:'video/mp4',buffer:Buffer.from('local fixture media')});
      await page.locator('#'+p+'Profile option[value="plugin-expert30"]').waitFor({state:'attached'});
      await page.locator('#'+p+'Profile').selectOption('plugin-expert30');
      await page.locator('#'+p+'Prompt').fill('保持素材');
      await page.locator(role==='admin'?'#genBtn':'#memberSubmit').click();
      await page.locator('#'+p+'Status').filter({hasText:'批次'}).waitFor();
      const request=requests.findLast(r=>r.path===(role==='admin'?'/api/admin/generate':'/v1/videos/generations'));
      assert.deepEqual(request.body.reference_media_uploads,['fixture-media']);
    }finally{await browser.close();}
  }
});
test('expert fifteen seconds survives duration changes and submits while ordinary Fast keeps fifteen hidden',async()=>{
  for(const role of ['admin','member']){
    const {browser,page,requests,errors}=await fixture(role),p=role==='admin'?'gen':'member';
    try{
      await page.locator('#'+p+'Profile option[value="plugin-expert30"]').waitFor({state:'attached'});
      assert.equal(await page.locator('#'+p+'Duration option[value="15"]').count(),0);
      await page.locator('#'+p+'Profile').selectOption('plugin-expert30');
      await page.locator('#'+p+'Model').selectOption('seedance-2.0');
      for(const duration of ['5','10','15','30'])await page.locator('#'+p+'Duration').selectOption(duration);
      await page.locator('#'+p+'TwoPart').check();
      await page.locator('#'+p+'Duration').selectOption('15');
      assert.equal(await page.locator('#'+p+'TwoPart').isHidden(),true);
      assert.equal(await page.locator('#'+p+'TwoPart').isChecked(),false);
      await page.locator('#'+p+'Prompt').fill('精确十五秒');
      await page.locator(role==='admin'?'#genBtn':'#memberSubmit').click();
      await page.locator('#'+p+'Status').filter({hasText:'批次'}).waitFor();
      const request=requests.findLast(r=>r.path===(role==='admin'?'/api/admin/generate':'/v1/videos/generations'));
      assert.equal(request.body.duration,15);assert.notEqual(request.body.two_part,true);
      assert.deepEqual(errors,[]);
    }finally{await browser.close();}
  }
});
test('only the administrator can preserve a single expert window and batches clear the option',async()=>{
  for(const role of ['admin','member']){
    const {browser,page,requests}=await fixture(role),p=role==='admin'?'gen':'member';
    try{
      await page.locator('#'+p+'Profile option[value="plugin-expert30"]').waitFor({state:'attached'});
      await page.locator('#'+p+'Profile').selectOption('plugin-expert30');
      if(role==='member'){assert.equal(await page.locator('#memberKeepBrowserOpen').count(),0);continue;}
      const keep=page.locator('#genKeepBrowserOpen');
      await keep.check();await page.locator('#genBatchCount').fill('2');
      assert.equal(await keep.isHidden(),true);assert.equal(await keep.isChecked(),false);
      await page.locator('#genBatchCount').fill('1');await keep.check();
      await page.locator('#genPrompt').fill('保留窗口');await page.locator('#genBtn').click();
      await page.locator('#genStatus').filter({hasText:'批次'}).waitFor();
      assert.equal(requests.findLast(r=>r.path==='/api/admin/generate').body.keep_browser_open,true);
    }finally{await browser.close();}
  }
});
test('member API example includes the selected expert parameters',async()=>{
  const {browser,page}=await fixture('member');
  try{
    await page.locator('#memberProfile option[value="plugin-expert30"]').waitFor({state:'attached'});
    await page.locator('#memberProfile').selectOption('plugin-expert30');
    await page.locator('#memberRatio').selectOption('9:16');await page.locator('#memberResolution').selectOption('1080p');
    await page.locator('#memberTwoPart').check();
    const example=await page.locator('#memberExample').textContent();
    assert.match(example,/ratio = '9:16'/);assert.match(example,/resolution = '1080p'/);assert.match(example,/two_part = \$true/);
  }finally{await browser.close();}
});
test('ordinary five seconds is single request while ten seconds and expert30 allow batches',async()=>{
  for(const role of ['admin','member']){
    const {browser,page,requests,errors}=await fixture(role);const p=role==='admin'?'gen':'member';
    try{
      const count=page.locator('#'+p+'BatchCount'),duration=page.locator('#'+p+'Duration');
      await duration.locator('option[value="5"]').waitFor({state:'attached'});
      await count.fill('2');await duration.selectOption('5');
      assert.equal(await count.inputValue(),'1');assert.equal(await count.isDisabled(),true);
      assert.match(await count.locator('..').innerText(),/仅支持单次/);
      await duration.selectOption('10');assert.equal(await count.isDisabled(),false);await count.fill('2');
      await page.locator('#'+p+'Profile').selectOption('plugin-expert30');
      assert.equal(await count.isDisabled(),false);assert.equal(await count.getAttribute('max'),'20');
      assert.equal(requests.some(r=>r.path.endsWith('/generate')||r.path.endsWith('/generations')),false);
      assert.deepEqual(errors,[]);
    }finally{await browser.close();}
  }
});
