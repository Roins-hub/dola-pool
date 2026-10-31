const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const source = fs.readFileSync('web/dola-native30.js', 'utf8');
// The current response contains JSON inside template strings at two levels.
const fast = {id:4,display_text:'Dreamina Seedance 2.0 Fast',selected_display_text:'2.0 Fast',option_key:'seedance_v2.0',extra:{sub_display:'Hohe Qualität für schnelle Ergebnisse'}};
const model = {key:'video-model',option_list:[fast,{id:1,display_text:'Dreamina Seedance 1.0',option_key:'ic_mini'}],default_selection_id:4,default_selection_index:0};
const duration = {key:'video-duration',option_list:[{id:1,option_key:'5',display_text:'5s'},{id:2,option_key:'10',display_text:'10s'}],default_selection_id:2};
const payload = {code:0,data:{language:'de',item_list:{video:{instruction_conf:{instruction_item_list:[{template:JSON.stringify({selector_list:[{template:JSON.stringify(model)},{template:JSON.stringify(duration)}]})}]}}},
 legacy:{option_list:[{value:'model',options:[{value:'seedance_v2.0',show_name:'Seedance 2.0 Fast',is_default:true,subscription_tag:'none'},{value:'ic_mini',show_name:'Seedance 1.0',is_default:false}]}],model_capability:{'seedance_v2.0':{supported_durations:['5','10'],supported_resolutions:['720p'],support_video_ref:false}}}}};
function setup(target,data=payload){
 const calls=[];
 class XHR {open(){this.readyState=4;this.raw=JSON.stringify(data);}get responseText(){return this.raw;}get response(){return this.responseType==='json'?JSON.parse(this.raw):this.raw;}}
 const ctx={URL,Response,Headers,XMLHttpRequest:XHR,location:{href:'https://www.dola.com/chat'},__DOLA_NATIVE_DURATION_TARGET__:target,
  fetch:async(...args)=>{calls.push(args);return new Response(JSON.stringify(data));}};
 ctx.window=ctx;vm.runInNewContext(source,ctx);return {ctx,calls,XHR};
}
function verify(data,target){
 data=JSON.parse(JSON.stringify(data));
 const selectors=JSON.parse(data.data.item_list.video.instruction_conf.instruction_item_list[0].template).selector_list.map(s=>JSON.parse(s.template));
 const option=selectors[0].option_list.find(o=>o.option_key==='seedance_v2.5');
 assert.ok(option,'The real Dola model selector must include the missing Seedance 2.5 option');
 assert.equal(option.display_text,'Dreamina Seedance 2.5');assert.equal(option.selected_display_text,'2.5');
 assert.notEqual(option.id,fast.id);assert.deepEqual(selectors[0].option_list.find(o=>o.option_key==='seedance_v2.0'),fast);
 assert.equal(selectors[0].default_selection_id,option.id);
 assert.equal(selectors[1].option_list[selectors[1].default_selection_index].option_key,String(target));
 const legacy=data.data.legacy.option_list[0].options;
 assert.equal(legacy.filter(o=>o.value==='seedance_v2.5').length,1);
 assert.equal(legacy.find(o=>o.is_default).value,'seedance_v2.5');
 assert.ok(data.data.legacy.model_capability['seedance_v2.5'].supported_durations.includes(String(target)));
 assert.deepEqual(data.data.legacy.model_capability['seedance_v2.0'],payload.data.legacy.model_capability['seedance_v2.0']);
 assert.equal(data.data.language,'de');return data;
}
(async()=>{
 for(const target of [5,10,15,30]){
  const {ctx,calls,XHR}=setup(target);
  const data=verify(await(await ctx.fetch('https://www.dola.com/alice/slot/action_bar_v3/get_item_conf')).json(),target);
  const repeat=setup(target,data);verify(await(await repeat.ctx.fetch('https://www.dola.com/samantha/skill/pack')).json(),target);
  const xhr=new XHR();xhr.open('GET','https://www.dola.com/samantha/skill/pack');verify(JSON.parse(xhr.responseText),target);xhr.responseType='json';verify(xhr.response,target);
  const init={method:'POST',body:'{"chat_ability":{"ability_param":"signed-original-body"}}',headers:{signature:'original'}};
  await ctx.fetch('https://www.dola.com/chat/completion',init);assert.equal(calls.at(-1)[1],init);
  assert.deepEqual(await(await ctx.fetch('https://other.test/samantha/skill/pack')).json(),payload);
 }
 console.log('model25 UI: missing option restored in both live config formats; short/long selections and signed request preserved');
})().catch(e=>{console.error(e);process.exitCode=1;});
