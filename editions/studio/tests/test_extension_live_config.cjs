const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const listener = {addListener(){}};
const calls = [];
const live = {code:0, data:{locale:'de-DE', new_server_field:{enabled:true}, video_generation:{meta:{title:'Videos erstellen'}, items:[{key:'duration',option_list:[{id:1,option_key:'5'},{id:2,option_key:'10'}]}]}}};
const context = {console,TextEncoder,TextDecoder,Uint8Array, btoa:s=>Buffer.from(s,'binary').toString('base64'),atob:s=>Buffer.from(s,'base64').toString('binary'),
 fetch:async()=>{throw new Error('Must not replace live config with bundled file');},
 chrome:{runtime:{onInstalled:listener,onStartup:listener,onMessage:listener,getURL:s=>s},tabs:{onActivated:listener,onUpdated:listener,onRemoved:listener},action:{onClicked:listener},debugger:{onDetach:listener,onEvent:listener,sendCommand(target,method,params,cb){calls.push({method,params});cb(method==='Fetch.getResponseBody'?{body:JSON.stringify(live),base64Encoded:false}:{});}}}};
vm.createContext(context);
vm.runInContext(fs.readFileSync('extensions/dola30/service-worker.js','utf8'),context);
(async()=>{
 assert.equal(vm.runInContext('fetchPatterns.find(p=>p.urlPattern.includes(DOLA_SKILL_PACK_URL_PART)).requestStage',context),'Response');
 await context.handlePausedRequest(1,{requestId:'ok',request:{url:'https://www.dola.com/samantha/skill/pack',method:'GET'},responseStatusCode:200,responseHeaders:[]});
 const result=calls.find(c=>c.method==='Fetch.fulfillRequest');
 const body=JSON.parse(Buffer.from(result.params.body,'base64').toString('utf8'));
 assert.equal(body.data.locale,'de-DE');assert.equal(body.data.video_generation.meta.title,'Videos erstellen');
 assert.deepEqual(body.data.new_server_field,live.data.new_server_field);
 assert.ok(body.data.video_generation.items[0].option_list.some(o=>o.option_key==='30'));
 for(const event of [{responseStatusCode:503},{responseStatusCode:204},{responseErrorReason:'ConnectionReset'}]){
  calls.length=0;
  await context.handlePausedRequest(1,{requestId:'error',request:{url:'https://www.dola.com/samantha/skill/pack',method:'GET'},...event});
  assert.deepEqual(calls.map(c=>c.method),['Fetch.continueRequest']);
 }
 console.log('live config: language/schema preserved, duration retained, network errors passed through');
})().catch(e=>{console.error(e);process.exitCode=1;});
