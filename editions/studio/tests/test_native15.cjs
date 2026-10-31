const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const source = fs.readFileSync('web/dola-native30.js', 'utf8');
const payload = {
  language: 'de', title: 'Video generieren',
  nested: JSON.stringify({items: [
    {key:'video-model', option_list:[{id:7,option_key:'seedance_v2.0'},{id:9,option_key:'seedance_v2.5'}]},
    {key:'video-duration', option_list:[{id:21,option_key:'10',display_text:'10s'},{id:22,option_key:'5',display_text:'5s'}]},
  ]}),
  legacy: {value:'duration', options:[{value:'5',show_name:'5s'}, {value:'10',show_name:'10s',is_default:true}]},
  model_capability: {'seedance_v2.5':{supported_durations:['5','10']}, 'seedance_v2.0':{supported_durations:['5','10']}},
};
function setup(target, data=payload) {
  const calls=[];
  class XHR {
    open(method,url){this.url=url;this.raw=JSON.stringify(data);this.readyState=4;}
    get responseText(){return this.raw;}
    get response(){return this.responseType==='json'?JSON.parse(this.raw):this.raw;}
  }
  const ctx={location:{href:'https://www.dola.com/chat'}, URL, Response, Headers, XMLHttpRequest:XHR,
    fetch:async(...args)=>{calls.push(args);return new Response(JSON.stringify(data));},
    __DOLA_NATIVE_DURATION_TARGET__:target};
  ctx.window=ctx;ctx.globalThis=ctx;
  vm.runInNewContext(source,ctx);
  return {ctx,calls,XHR};
}
function verify15(data) {
  data=JSON.parse(JSON.stringify(data)); // XHR JSON objects cross the VM realm.
  const items=JSON.parse(data.nested).items, duration=items[1];
  assert.equal(duration.option_list[duration.default_selection_index].option_key,'15');
  assert.equal(duration.default_selection_id,duration.option_list[duration.default_selection_index].id);
  assert.equal(items[0].default_selection_id,9);
  assert.equal(data.legacy.options.filter(x=>x.is_default).length,1);
  assert.equal(data.legacy.options.find(x=>x.is_default).value,'15');
  assert.deepEqual(data.model_capability['seedance_v2.5'].supported_durations,['5','10','15']);
  assert.deepEqual(data.model_capability['seedance_v2.0'].supported_durations,['5','10']);
  assert.equal(data.language,'de');assert.equal(data.title,'Video generieren');
  assert.ok(!duration.option_list.some(x=>x.option_key==='30'),'15-second setup must not add 30');
}
(async()=>{
  const {ctx,calls,XHR}=setup(15);
  const result=await (await ctx.fetch('https://www.dola.com/alice/slot/action_bar_v3/get_item_conf')).json();
  verify15(result);
  const repeated=setup(15,result);
  const repeatedData=await (await repeated.ctx.fetch('https://www.dola.com/samantha/skill/pack')).json();
  verify15(repeatedData);
  assert.equal(JSON.parse(repeatedData.nested).items[1].option_list.filter(x=>x.option_key==='15').length,1);
  const xhr=new XHR();xhr.open('GET','https://www.dola.com/samantha/skill/pack');
  verify15(JSON.parse(xhr.responseText));xhr.responseType='json';verify15(xhr.response);
  const body=JSON.stringify({prompt:'原始提示词',chat_ability:{ability_param:JSON.stringify({model:'seedance_v2.5',duration:15})}});
  const init={method:'POST',body,headers:{signature:'unchanged'}};
  await ctx.fetch('https://www.dola.com/chat/completion',init);
  assert.equal(calls.at(-1)[1],init);assert.equal(calls.at(-1)[1].body,body);
  xhr.open('POST','https://www.dola.com/chat/completion');assert.deepEqual(xhr.response,payload);
  for (const target of [undefined,25]) {
    const inactive=setup(target);
    assert.deepEqual(await(await inactive.ctx.fetch('https://www.dola.com/samantha/skill/pack')).json(),payload);
  }
  assert.deepEqual(await(await ctx.fetch('https://other.test/samantha/skill/pack')).json(),payload);
  console.log('native15: 15 selected in fetch/XHR configuration; Fast, locale and submission body preserved');
})().catch(e=>{console.error(e);process.exitCode=1;});
