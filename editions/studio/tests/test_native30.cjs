const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const source = fs.readFileSync('web/dola-native30.js', 'utf8');
const payload = {nested: JSON.stringify({items: [
  {key:'video-model', option_list:[{id:7,option_key:'seedance_v2.0'},{id:9,option_key:'seedance_v2.5'}]},
  {key:'video-duration',option_list:[{id:21,option_key:'10'},{id:22,option_key:'5'}]},
]}), model_capability:{'seedance_v2.5':{supported_durations:['5','10']}}};
let calls=[];
class XHR {
  open(method,url){this.url=url;this.raw=JSON.stringify(payload);this.readyState=4;}
  get responseText(){return this.raw;}
  get response(){return this.responseType==='json'?JSON.parse(this.raw):this.raw;}
}
const context = {location:{href:'https://www.dola.com/chat'}, URL, Response, Headers,
  XMLHttpRequest:XHR, fetch: async (...args)=>{calls.push(args);return new Response(JSON.stringify(payload));},
  __DOLA_NATIVE30_TARGET__:30};
context.window=context;context.globalThis=context;
vm.runInNewContext(source,context);
(async()=>{
  let response = await context.fetch('https://www.dola.com/alice/slot/action_bar_v3/get_item_conf');
  let data=await response.json(), items=JSON.parse(data.nested).items;
  let duration=items[1], selected=duration.option_list[duration.default_selection_index];
  assert.equal(selected.option_key,'30');
  assert.equal(duration.default_selection_id,selected.id);
  assert.equal(items[0].default_selection_id,9);
  assert.ok(data.model_capability['seedance_v2.5'].supported_durations.includes('30'));
  assert.equal(payload.model_capability['seedance_v2.5'].supported_durations.length,2);
  const raw='{"prompt":"用户原文","duration":30}';
  const init={method:'POST',body:raw,headers:{signature:'untouched'}};
  await context.fetch('https://www.dola.com/chat/completion', init);
  assert.equal(calls.at(-1)[1],init);
  assert.equal(calls.at(-1)[1].body,raw);
  response=await context.fetch('https://other.test/samantha/skill/pack');
  assert.deepEqual(await response.json(),payload);
  const xhr=new XHR();xhr.open('GET','https://www.dola.com/samantha/skill/pack');
  assert.ok(JSON.parse(xhr.responseText).model_capability['seedance_v2.5'].supported_durations.includes('30'));
  xhr.open('POST','https://www.dola.com/chat/completion');
  assert.deepEqual(JSON.parse(xhr.responseText),payload);
  console.log('native30: fetch/XHR config patched; signed submission unchanged');
})().catch(e=>{console.error(e);process.exitCode=1;});
