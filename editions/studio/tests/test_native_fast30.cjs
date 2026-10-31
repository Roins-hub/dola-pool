const assert=require('node:assert/strict'),fs=require('node:fs'),vm=require('node:vm');
const {test}=require('node:test');
const source=fs.readFileSync('web/dola-native30.js','utf8');
const clone=value=>JSON.parse(JSON.stringify(value));
const payload={locale:'de-DE',model_capability:{'seedance_v2.0':{supported_durations:['5','10']},
  'seedance_v2.5':{supported_durations:['10','30']},ic_mini:{supported_durations:['5','10','15']}},
  legacy:{value:'model',options:[{value:'seedance_v2.0',show_name:'Fast'},
    {value:'seedance_v2.5',show_name:'2.5',is_default:true},{value:'seedance_v2.0_std',show_name:'Standard'}]},
  duration:{value:'duration',options:[{value:'5'},{value:'10',is_default:true}]},
  range:{key:'video-duration',lower_bound:4,upper_bound:10,step_length:1,default_selected_duration:10},
  nested:JSON.stringify({items:[{key:'video-model',option_list:[{id:1,option_key:'seedance_v2.0'},
    {id:2,option_key:'seedance_v2.5'},{id:3,option_key:'seedance_v2.0_std'}]},
    {key:'video-duration',option_list:[{id:4,option_key:'5'},{id:5,option_key:'10'}]}]})};
function setup(data=payload){
  const calls=[];
  class XHR{open(){this.readyState=4;this.raw=JSON.stringify(data);}
    get responseText(){return this.raw;}get response(){return this.responseType==='json'?JSON.parse(this.raw):this.raw;}}
  const ctx={URL,Headers,Response,XMLHttpRequest:XHR,location:{href:'https://www.dola.com/chat'},
    __DOLA_NATIVE_DURATION_TARGET__:30,__DOLA_NATIVE_MODEL_TARGET__:'seedance_v2.0',
    fetch:async(...args)=>{calls.push(args);return new Response(JSON.stringify(data));}};
  ctx.window=ctx;vm.runInNewContext(source,ctx);return{ctx,calls,XHR};
}
function verify(data){
  data=clone(data);const items=JSON.parse(data.nested).items;
  assert.equal(items[0].default_selection_id,1,'30s must still select Fast 2.0');
  assert.equal(data.legacy.options.find(x=>x.is_default)?.value,'seedance_v2.0');
  assert.equal(data.duration.options.find(x=>x.is_default)?.value,'30');
  assert.equal(items[1].option_list[items[1].default_selection_index]?.option_key,'30');
  for(const duration of ['15','30']){
    assert.equal(data.duration.options.filter(x=>x.value===duration).length,1);
    assert.equal(items[1].option_list.filter(x=>x.option_key===duration).length,1);
  }
  assert.deepEqual(data.model_capability['seedance_v2.0'].supported_durations,['5','10','15','30']);
  assert.deepEqual(data.model_capability['seedance_v2.5'],payload.model_capability['seedance_v2.5']);
  assert.deepEqual(data.model_capability.ic_mini,payload.model_capability.ic_mini);
  assert.equal(data.range.upper_bound,30);assert.equal(data.range.default_selected_duration,30);
  assert.equal(data.range.lower_bound,4);assert.equal(data.range.step_length,1);assert.equal(data.locale,'de-DE');return data;
}
test('Fast30 experiment config keeps Fast selected, adds 15/30 once and preserves other model capabilities',async()=>{
  const {ctx,XHR}=setup();const data=verify(await(await ctx.fetch('https://www.dola.com/samantha/skill/pack')).json());
  const repeated=setup(data);verify(await(await repeated.ctx.fetch('https://www.dola.com/alice/slot/action_bar_v3/get_item_conf')).json());
  const xhr=new XHR();xhr.open('GET','https://www.dola.com/samantha/skill/pack');verify(JSON.parse(xhr.responseText));
  xhr.responseType='json';verify(xhr.response);
});
test('Fast30 experiment config never rewrites a signed request or unrelated response',async()=>{
  const {ctx,calls,XHR}=setup();
  const init={method:'POST',body:'{"model":"seedance_v2.0","duration":30,"prompt":"用户原文"}',headers:{signature:'original'}};
  const result=await ctx.fetch('https://www.dola.com/chat/completion?a_bogus=unchanged',init);
  assert.equal(calls.at(-1)[1],init);assert.deepEqual(await result.json(),payload);
  assert.deepEqual(await(await ctx.fetch('https://other.test/samantha/skill/pack')).json(),payload);
  const xhr=new XHR();xhr.open('POST','https://www.dola.com/chat/completion');assert.deepEqual(JSON.parse(xhr.responseText),payload);
});
