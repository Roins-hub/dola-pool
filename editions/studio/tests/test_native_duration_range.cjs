const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const {test} = require('node:test');
const source = fs.readFileSync('web/dola-native30.js', 'utf8');
const clone = value => JSON.parse(JSON.stringify(value));
const fast = 'seedance_v2.0';
function setup(data, model=fast, target=15) {
  const calls = [];
  class XHR {
    open(){this.readyState=4;this.raw=JSON.stringify(data);}
    get responseText(){return this.raw;}
    get response(){return this.responseType==='json'?JSON.parse(this.raw):this.raw;}
  }
  const ctx={URL,Headers,Response,XMLHttpRequest:XHR,location:{href:'https://www.dola.com/chat'},
    __DOLA_NATIVE_DURATION_TARGET__:target,__DOLA_NATIVE_MODEL_TARGET__:model,
    fetch:async(...args)=>{calls.push(args);return new Response(JSON.stringify(data));}};
  ctx.window=ctx;vm.runInNewContext(source,ctx);
  return {ctx,calls,XHR,read:async()=>clone(await(await ctx.fetch('https://www.dola.com/samantha/skill/pack')).json())};
}
test('Fast15 range raises a snake-case 10s bound to 15 and replaces its default',async()=>{
  const range={key:'video-duration',lower_bound:4,upper_bound:10,step_length:1,
    default_selected_duration:10,display_text:'Duration',unit:'seconds'};
  const {read}=setup(range),data=await read();
  assert.deepEqual(data,{...range,upper_bound:15,default_selected_duration:15});
  assert.deepEqual(range.upper_bound,10,'Do not mutate the original response fixture');
});
test('Fast15 patches camel-case or mixed range fields without adding another naming convention',async()=>{
  for(const [range,expected] of [
    [{key:'video-duration',lowerBound:4,upperBound:10,stepLength:1,defaultSelectedDuration:5},
      {key:'video-duration',lowerBound:4,upperBound:15,stepLength:1,defaultSelectedDuration:15}],
    [{key:'video-duration',lowerBound:4,stepLength:1},
      {key:'video-duration',lowerBound:4,stepLength:1,upperBound:15,defaultSelectedDuration:15}],
    [{key:'video-duration',lower_bound:4,step_length:1},
      {key:'video-duration',lower_bound:4,step_length:1,upper_bound:15,default_selected_duration:15}],
    [{key:'video-duration',upper_bound:10,upperBound:30,default_selected_duration:5,defaultSelectedDuration:10},
      {key:'video-duration',upper_bound:15,upperBound:15,default_selected_duration:15,defaultSelectedDuration:15}],
  ]) assert.deepEqual(await setup(range).read(),expected);
});
test('Range max stays within the selected model whitelist and existing valid upper bounds are preserved',async()=>{
  for(const [model,target,originalMax,expectedMax] of [
    [fast,15,30,15],[fast,30,10,30],['ic_mini',15,30,15],['seedance_v2.5',30,10,30],
    ['seedance_v2.5',15,30,30],[fast,5,10,10],['ic_mini',10,10,10],
  ]) {
    const data=await setup({key:'video-duration',upper_bound:originalMax,default_selected_duration:5},model,target).read();
    assert.equal(data.upper_bound,expectedMax,`${model}/${target}`);
    assert.equal(data.default_selected_duration,target);
  }
  const invalid={key:'video-duration',upper_bound:10,default_selected_duration:10};
  assert.deepEqual(await setup(invalid,fast,20).read(),invalid,'Fast rejects unconfigured experiment durations');
  assert.deepEqual(await setup(invalid,'ic_mini',30).read(),invalid,'1.0 never enables 30s');
});
test('Nested serialized ranges work for fetch and XHR and only the target model capability expands',async()=>{
  const fixture={locale:'de-DE',model_capability:{[fast]:{supported_durations:['5','10']},
    'seedance_v2.5':{supported_durations:['10','30']},ic_mini:{supported_durations:['5','10']}},
    conf:JSON.stringify({items:[{key:'video-duration',lower_bound:4,upper_bound:10,default_selected_duration:10}]})};
  function verify(data) {
    data=clone(data);
    const range=JSON.parse(data.conf).items[0];
    assert.equal(range.upper_bound,15);assert.equal(range.default_selected_duration,15);
    assert.deepEqual(data.model_capability[fast].supported_durations,['5','10','15']);
    assert.deepEqual(data.model_capability['seedance_v2.5'],fixture.model_capability['seedance_v2.5']);
    assert.deepEqual(data.model_capability.ic_mini,fixture.model_capability.ic_mini);
    assert.equal(data.locale,'de-DE');return data;
  }
  const {read,XHR}=setup(fixture),data=verify(await read());
  verify(await setup(data).read());
  const xhr=new XHR();xhr.open('GET','https://www.dola.com/alice/slot/action_bar_v3/get_item_conf');
  verify(JSON.parse(xhr.responseText));xhr.responseType='json';verify(xhr.response);
});
test('Unrelated sliders and dropdown metadata are not treated as duration ranges',async()=>{
  const unrelated={key:'image-width',lower_bound:4,upper_bound:10,default_selected_duration:10};
  const noRange={key:'video-duration',display_text:'Duration'};
  const dropdown={key:'video-duration',upper_bound:10,option_list:[{id:1,option_key:'10'}]};
  const data=await setup({items:[unrelated,noRange,dropdown]}).read();
  assert.deepEqual(data.items[0],unrelated);assert.deepEqual(data.items[1],noRange);
  assert.equal(data.items[2].upper_bound,10,'An option list must stay on the existing dropdown path');
  assert.equal(data.items[2].option_list[data.items[2].default_selection_index].option_key,'15');
});
test('Range patch never rewrites signed request bodies, external endpoints or response bodies',async()=>{
  const fixture={key:'video-duration',upper_bound:10,default_selected_duration:10};
  const {ctx,calls,XHR}=setup(fixture);
  const init={method:'POST',body:'{"chat_ability":{"ability_param":"{\\"model\\":\\"seedance_v2.0\\",\\"duration\\":15}"}}',
    headers:{signature:'original'}};
  const response=await ctx.fetch('https://www.dola.com/chat/completion?a_bogus=unchanged',init);
  assert.equal(calls.at(-1)[1],init);assert.deepEqual(await response.json(),fixture);
  assert.deepEqual(await(await ctx.fetch('https://other.test/samantha/skill/pack')).json(),fixture);
  const xhr=new XHR();xhr.open('POST','https://www.dola.com/chat/completion');
  assert.deepEqual(JSON.parse(xhr.responseText),fixture);
});
