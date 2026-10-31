// Network-free adapter around the unchanged 1.6.60 resource parser.
const fs=require('fs'),vm=require('vm'),path=require('path'),{webcrypto}=require('crypto');
const messages=[],event={addListener(){}};
async function main(){
  const input=JSON.parse(fs.readFileSync(0,'utf8'));
  if(input.action==='parameter_prompt'||input.action==='expert_protocol'){
    const source=fs.readFileSync(path.join(__dirname,'extensions','dola30-plugin1660','dola-silent-expert-main.js'),'utf8');
    const start=source.indexOf('function parameterPrompt(context)'),end=source.indexOf('function batchActive()',start);
    if(start<0||end<0)throw Error('Fixed protocol function missing');
    const sandbox={object:v=>v!==null&&typeof v==='object'&&!Array.isArray(v)};
    vm.createContext(sandbox);vm.runInContext(source.slice(start,end)+';this.prefix=parameterPrompt;',sandbox,{timeout:1000});
    const prefix=sandbox.prefix(input.context);
    if(input.action==='parameter_prompt'){
      process.stdout.write(JSON.stringify({prefix}));return;
    }
    const twoPartSource=fs.readFileSync(path.join(__dirname,'extensions','dola30-plugin1660','two-part-main.js'),'utf8');
    const constants=twoPartSource.slice(twoPartSource.indexOf('const MARK ='),twoPartSource.indexOf('const object ='));
    const addStart=twoPartSource.indexOf('function add(text)'),addEnd=twoPartSource.indexOf('function content(',addStart);
    if(!constants||addStart<0||addEnd<0)throw Error('Fixed two-part rule missing');
    vm.runInContext(constants+'\n'+twoPartSource.slice(addStart,addEnd)+';this.twoPartAdd=add;this.twoPartRule=RULE;',sandbox,{timeout:1000});
    const draft=input.two_part===true?sandbox.twoPartAdd(input.prompt):input.prompt;
    process.stdout.write(JSON.stringify({prefix,draft_text:draft,expected_text:prefix+draft,two_part_rule:sandbox.twoPartRule}));return;
  }
  const tabUrl='https://www.dola.com/chat/'+String(input.conversation_id||'0');
  const context={URL,URLSearchParams,TextEncoder,TextDecoder,Uint8Array,ArrayBuffer,DataView,atob,btoa,AbortController,setTimeout,clearTimeout,crypto:webcrypto,
    console:{warn(){},log(){},error(){}},importScripts(){},
    chrome:{runtime:{id:'project-original-parser',onInstalled:event,onStartup:event,onMessage:event},
      tabs:{onActivated:event,onUpdated:event,onRemoved:event,query:async()=>[],get:async()=>({id:1,url:tabUrl}),sendMessage:async(id,message)=>messages.push(message)},
      action:{onClicked:event},downloads:{onDeterminingFilename:event}},
    fetch:async(url)=>{const saved=(input.fallback_responses||[]).find(r=>r.request_url===String(url));
      if(!saved)throw Error('No validated response supplied');
      return {ok:saved.status===200,status:saved.status,url:saved.response_url||saved.request_url,json:async()=>saved.payload};}
  };
  vm.createContext(context);
  const source=fs.readFileSync(path.join(__dirname,'extensions','dola30-plugin1660','service-worker.js'),'utf8');
  vm.runInContext(source,context,{filename:'fixed-original-plugin-service-worker.js',timeout:3000});
  const apis=(input.fallback_apis||[]).slice(0,20),models=(input.video_models||[]).slice(0,20);
  const requests=apis.map(url=>({original_url:url,request_url:context.replaceQueryParams(url,{channel:'no',codec_type:'8',logo_type:'unwatermarked'})}));
  const modelReport=await context.resolveVideoModelsForTab(1,tabUrl,models.map(v=>typeof v==='string'?v:JSON.stringify(v)),input.resolution||'auto');
  const fallbackResults=[];
  if(input.fallback_responses)for(const url of apis)fallbackResults.push(await context.getUnwatermarkedVideoResourceFromFallbackApi(url,'dola',input.resolution||'auto'));
  process.stdout.write(JSON.stringify({model_report:modelReport,model_resources:messages.flatMap(m=>m.type==='MEDIA_FOUND'?m.items||[]:[]),fallback_requests:requests,fallback_results:fallbackResults}));
}
main().catch(()=>{process.stderr.write('Original-plugin resource parser failed');process.exitCode=1;});
