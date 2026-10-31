const assert = require('node:assert/strict'), fs = require('node:fs'), vm = require('node:vm');
const {test} = require('node:test');
const source = fs.readFileSync('web/dola-native30.js', 'utf8');
const payload = {locale:'de-DE', model_capability:{ic_mini:{supported_durations:['5','10']},
  'seedance_v2.0':{supported_durations:['5','10','15']}, 'seedance_v2.5':{supported_durations:['10','30']}},
  model:{value:'model',options:[{value:'ic_mini',show_name:'Seedance 1.0'},
    {value:'seedance_v2.0',is_default:true},{value:'seedance_v2.5'}]},
  duration:{value:'duration',options:[{value:'5'},{value:'10',is_default:true}]},
  nested:JSON.stringify({items:[{key:'video-model',option_list:[{id:1,option_key:'ic_mini'},
    {id:2,option_key:'seedance_v2.0'},{id:3,option_key:'seedance_v2.5'}]},
    {key:'video-duration',option_list:[{id:4,option_key:'5'},{id:5,option_key:'10'}]}]})};
function setup(target, model='ic_mini', data=payload) {
  const calls=[];
  class XHR {open(){this.readyState=4;this.raw=JSON.stringify(data);}
    get responseText(){return this.raw;}
    get response(){return this.responseType==='json'?JSON.parse(this.raw):this.raw;}}
  const ctx={URL,Headers,Response,XMLHttpRequest:XHR,location:{href:'https://www.dola.com/chat'},
    __DOLA_NATIVE_DURATION_TARGET__:target,__DOLA_NATIVE_MODEL_TARGET__:model,
    fetch:async(...args)=>{calls.push(args);return new Response(JSON.stringify(data));}};
  ctx.window=ctx;vm.runInNewContext(source,ctx);return {ctx,calls,XHR};
}
function verify(data, duration) {
  data=JSON.parse(JSON.stringify(data));
  assert.equal(data.model.options.find(x=>x.is_default)?.value,'ic_mini');
  assert.equal(data.duration.options.find(x=>x.is_default)?.value,String(duration));
  const items=JSON.parse(data.nested).items;
  assert.equal(items[0].option_list[items[0].default_selection_index].option_key,'ic_mini');
  assert.equal(items[0].default_selection_id,1);
  assert.equal(items[1].option_list[items[1].default_selection_index].option_key,String(duration));
  const expectedCapabilities=JSON.parse(JSON.stringify(payload.model_capability));
  if(duration===15) expectedCapabilities.ic_mini.supported_durations.push('15');
  assert.deepEqual(data.model_capability,expectedCapabilities);
  assert.equal(data.locale,payload.locale);
  assert.equal(data.model.options.length,3);
  assert.equal(data.duration.options.length,duration===15?3:2);
}
test('Seedance 1.0 native config selects ic_mini at 5/10s, preserves capabilities and signed requests',async()=>{
  for(const duration of [5,10]) {
    const {ctx,calls,XHR}=setup(duration);
    verify(await(await ctx.fetch('https://www.dola.com/samantha/skill/pack')).json(),duration);
    const xhr=new XHR();xhr.open('GET','https://www.dola.com/alice/slot/action_bar_v3/get_item_conf');
    verify(JSON.parse(xhr.responseText),duration);xhr.responseType='json';verify(xhr.response,duration);
    const init={method:'POST',body:'{"model":"ic_mini","duration":'+duration+'}',headers:{signature:'original'}};
    await ctx.fetch('https://www.dola.com/chat/completion',init);assert.equal(calls.at(-1)[1],init);
    assert.deepEqual(await(await ctx.fetch('https://other.test/samantha/skill/pack')).json(),payload);
  }
});
test('Seedance 1.0 experimental 15s selects ic_mini and changes only its target duration capability',async()=>{
  const {ctx,calls,XHR}=setup(15);
  const data=await(await ctx.fetch('https://www.dola.com/samantha/skill/pack')).json();
  verify(data,15);
  const repeat=setup(15,'ic_mini',data);
  verify(await(await repeat.ctx.fetch('https://www.dola.com/alice/slot/action_bar_v3/get_item_conf')).json(),15);
  const xhr=new XHR();xhr.open('GET','https://www.dola.com/samantha/skill/pack');
  verify(JSON.parse(xhr.responseText),15);xhr.responseType='json';verify(xhr.response,15);
  const init={method:'POST',body:'{"model":"ic_mini","duration":15,"prompt":"用户原文"}',headers:{signature:'original'}};
  await ctx.fetch('https://www.dola.com/chat/completion',init);assert.equal(calls.at(-1)[1],init);
});
test('Seedance 1.0 does not activate for 30s or ambiguous model labels',async()=>{
  for(const [duration,model] of [[30,'ic_mini'],[10,'mini'],[10,'fast'],[10,'seedance_v2.0_std']]) {
    const {ctx}=setup(duration,model);
    assert.deepEqual(await(await ctx.fetch('https://www.dola.com/samantha/skill/pack')).json(),payload);
    assert.equal(ctx.__dolaNative30Installed,undefined);
  }
});
test('Seedance 1.0-only legacy menu can be selected without inventing other models',async()=>{
  const oneOnly=JSON.parse(JSON.stringify(payload));oneOnly.model.options=oneOnly.model.options.slice(0,1);
  const {ctx}=setup(5,'ic_mini',oneOnly);
  const data=await(await ctx.fetch('https://www.dola.com/samantha/skill/pack')).json();
  assert.equal(data.model.options[0].is_default,true);
  assert.equal(data.model.options.length,1);
});
