const assert=require('node:assert/strict'),fs=require('node:fs'),vm=require('node:vm');
const source=fs.readFileSync('web/dola-native30.js','utf8');
const payload={locale:'de-DE',model_capability:{'seedance_v2.0':{supported_durations:['5','10']},'seedance_v2.5':{supported_durations:['10','30']}},
  legacy:{value:'model',options:[{value:'seedance_v2.0',show_name:'Fast'},{value:'seedance_v2.5',show_name:'2.5',is_default:true}]},
  duration:{value:'duration',options:[{value:'5'},{value:'10',is_default:true}]},
  nested:JSON.stringify({items:[{key:'video-model',option_list:[{id:1,option_key:'seedance_v2.0'},{id:2,option_key:'seedance_v2.5'}]},
    {key:'video-duration',option_list:[{id:3,option_key:'5'},{id:4,option_key:'10'}]}]})};
function setup(data=payload,target=15){
  const calls=[];class XHR{open(){this.readyState=4;this.raw=JSON.stringify(data);}get responseText(){return this.raw;}get response(){return this.responseType==='json'?JSON.parse(this.raw):this.raw;}}
  const ctx={URL,Headers,Response,XMLHttpRequest:XHR,location:{href:'https://www.dola.com/chat'},__DOLA_NATIVE_DURATION_TARGET__:target,__DOLA_NATIVE_MODEL_TARGET__:'seedance_v2.0',
    fetch:async(...args)=>{calls.push(args);return new Response(JSON.stringify(data));}};ctx.window=ctx;vm.runInNewContext(source,ctx);return{ctx,calls,XHR};
}
function verify(data){
  data=JSON.parse(JSON.stringify(data));
  const items=JSON.parse(data.nested).items;
  assert.equal(items[0].default_selection_id,1,'15s must still select Fast');
  assert.equal(items[1].option_list[items[1].default_selection_index].option_key,'15');
  assert.equal(items[1].option_list.filter(x=>x.option_key==='15').length,1);
  assert.equal(data.legacy.options.find(x=>x.is_default).value,'seedance_v2.0');
  assert.equal(data.duration.options.find(x=>x.is_default).value,'15');
  assert.deepEqual(data.model_capability['seedance_v2.0'].supported_durations,['5','10','15']);
  assert.deepEqual(data.model_capability['seedance_v2.5'],payload.model_capability['seedance_v2.5']);
  assert.equal(data.locale,'de-DE');assert.ok(!items[1].option_list.some(x=>x.option_key==='30'));return data;
}
(async()=>{
  const {ctx,calls,XHR}=setup();const data=verify(await(await ctx.fetch('https://www.dola.com/samantha/skill/pack')).json());
  const repeat=setup(data);verify(await(await repeat.ctx.fetch('https://www.dola.com/alice/slot/action_bar_v3/get_item_conf')).json());
  const xhr=new XHR();xhr.open('GET','https://www.dola.com/samantha/skill/pack');verify(JSON.parse(xhr.responseText));xhr.responseType='json';verify(xhr.response);
  const init={method:'POST',body:'{"model":"seedance_v2.0","duration":15,"prompt":"用户原文"}',headers:{signature:'original'}};
  await ctx.fetch('https://www.dola.com/chat/completion',init);assert.equal(calls.at(-1)[1],init);
  const bad=setup(payload,20);assert.deepEqual(await(await bad.ctx.fetch('https://www.dola.com/samantha/skill/pack')).json(),payload);
  console.log('Fast15: Fast selected, 15s added once, 2.5 and signed submission preserved');
})().catch(error=>{console.error(error);process.exitCode=1;});
