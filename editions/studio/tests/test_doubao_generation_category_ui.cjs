/* Account routing on the real shared generation controls; no live requests. */
const {test}=require('node:test');
const assert=require('node:assert/strict');
const path=require('node:path');
const {chromium}=require('./browser-runtime.cjs');

async function fixture(run){
  const browser=await chromium.launch({headless:true});
  try{
    const page=await browser.newPage();
    const evaluate=page.evaluate.bind(page);
    page.evaluate=(fn,arg)=>evaluate(fn,arg,{},false);
    await page.route('**/*',route=>route.abort());
    await page.setContent('<div id="anchor"></div>');
    await page.addScriptTag({path:path.join(__dirname,'../web/platform-ui.js')});
    await page.evaluate(()=>{
      window.controls=PlatformUI.advanced({query:s=>document.querySelector(s),prefix:'test',anchor:'#anchor'});
      window.fast={id:'dialog_seedance_2.0_fast',entry_mode:'ordinary_dialog',features:{}};
      window.standard={id:'dialog_seedance_2.5',entry_mode:'ordinary_dialog',account_category:'标准账号',features:{}};
    });
    await run(page);
  }finally{await browser.close();}
}

test('Doubao generation defaults to ordinary accounts and includes that routing in the request',async()=>{
  await fixture(async page=>{
    await page.evaluate(()=>controls.render('doubao',fast));
    assert.equal(await page.locator('#testAccountCategory').count(),1,'an account pool selector must be available');
    assert.equal(await page.locator('#testAccountCategory').isVisible(),true);
    assert.equal(await page.locator('#testAdvanced').isHidden(),true,'unverified optional features remain hidden');
    assert.equal(await page.locator('#testAccountCategory').inputValue(),'普通账号');
    assert.deepEqual(await page.evaluate(()=>({...PlatformUI.request({platform:'doubao',model:fast.id,duration:15,prompt:'小猫推门',ratio:'16:9'}),...controls.values()})),{
      platform:'doubao',model:'dialog_seedance_2.0_fast',duration:15,prompt:'小猫推门',ratio:'16:9',account_category:'普通账号'
    });
  });
});

test('standard and automatic pools require an explicit selection that survives rerendering',async()=>{
  await fixture(async page=>{
    await page.evaluate(()=>controls.render('doubao',fast));
    assert.equal(await page.locator('#testAccountCategory').count(),1);
    await page.selectOption('#testAccountCategory','标准账号');
    await page.evaluate(()=>controls.render('doubao',fast,true));
    assert.equal(await page.locator('#testAccountCategory').isDisabled(),true);
    assert.deepEqual(await page.evaluate(()=>controls.values()),{account_category:'标准账号'});
    await page.evaluate(()=>controls.render('doubao',fast));
    await page.selectOption('#testAccountCategory','');
    await page.evaluate(()=>controls.render('doubao',fast));
    assert.deepEqual(await page.evaluate(()=>controls.values()),{account_category:null});
    assert.match(await page.locator('#testAccountCategory option:checked').innerText(),/自动选择/);
  });
});

test('a standard-only model cannot route to ordinary accounts and does not overwrite the Fast preference',async()=>{
  await fixture(async page=>{
    await page.evaluate(()=>{controls.render('doubao',fast);controls.render('doubao',standard);});
    assert.equal(await page.locator('#testAccountCategory').count(),1);
    assert.equal(await page.locator('#testAccountCategory').inputValue(),'标准账号');
    assert.equal(await page.locator('#testAccountCategory').isDisabled(),true);
    await page.evaluate(()=>document.querySelector('#testAccountCategory').value='普通账号');
    assert.deepEqual(await page.evaluate(()=>controls.values()),{account_category:'标准账号'});
    await page.evaluate(()=>controls.render('doubao',fast));
    assert.equal(await page.locator('#testAccountCategory').inputValue(),'普通账号');
    assert.equal(await page.locator('#testAccountCategory').isDisabled(),false);
  });
});

test('Dola never sends a Doubao category and returning to Doubao restores the explicit preference',async()=>{
  await fixture(async page=>{
    await page.evaluate(()=>controls.render('doubao',fast));
    assert.equal(await page.locator('#testAccountCategory').count(),1);
    await page.selectOption('#testAccountCategory','标准账号');
    await page.evaluate(()=>controls.render('dola',{id:'seedance-2.0'}));
    assert.equal(await page.locator('#testAccountCategory').isVisible(),false);
    assert.deepEqual(await page.evaluate(()=>controls.values()),{});
    await page.evaluate(()=>controls.render('doubao',fast));
    assert.deepEqual(await page.evaluate(()=>controls.values()),{account_category:'标准账号'});
  });
});

test('account routing composes with existing resolution and watermark controls',async()=>{
  await fixture(async page=>{
    await page.evaluate(()=>controls.render('doubao',{...fast,features:{resolution:{enabled:true,values:['720p','1080p']},no_watermark:{enabled:true}}}));
    await page.selectOption('#testResolution','1080p');
    await page.check('#testNoWatermark');
    assert.deepEqual(await page.evaluate(()=>controls.values()),{account_category:'普通账号',resolution:'1080p',require_no_watermark:true});
  });
});
