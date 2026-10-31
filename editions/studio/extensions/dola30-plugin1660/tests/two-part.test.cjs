const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const path = require('node:path');
const source = fs.readFileSync(path.join(__dirname, '..', 'two-part-main.js'), 'utf8');
function environment(site='dola') {
  const attrs = new Map(), storage = new Map(), listeners = new Map();
  const context = { location: { hostname: `www.${site}.com` }, Event: class { constructor(type) { this.type=type; } },
    localStorage: { getItem: key => storage.get(key), setItem: (key,value) => storage.set(key,value) },
    document: { documentElement: {setAttribute:(key,value)=>attrs.set(key,value),getAttribute:key=>attrs.get(key)},
      addEventListener:(key,fn)=>listeners.set(key,fn), dispatchEvent:event=>listeners.get(event.type)?.(event) } };
  vm.createContext(context); vm.runInContext(source,context);
  return {api:context.__DOUBAO_DOLA_TWO_PART__,attrs,
    enable(value=true){attrs.set('data-dola-two-part-enabled',value?'1':'0');listeners.get('dola-two-part-change')();}};
}
const params = () => ({model:'seedance_v2.0',resolution:'720p',duration:30,ratio:'16:9',input_box_content:JSON.stringify({user_input_content:'第01段 雨夜 @{角色.png}\n第02段 外卖'})});
const message = () => ({role:'user',content_blocks_v2:[{block_type:2,content:{url:'https://ref.test/asset',text:'attachment'}},
  {block_type:10000,content:{text_block:{text:'第01段 雨夜 @{角色.png}\n第02段 外卖'}}}],
  ext:{chat_ability:JSON.stringify({ability_type:17,ability_param:JSON.stringify(params())})}});
for (const site of ['dola','doubao']) test(`${site}: default off, append once, preserve references and parameters, disable again`,()=>{
  const env=environment(site), request={messages:[{role:'user',text:'历史'},message()]};
  const original=JSON.stringify(request);
  assert.strictEqual(env.api.prepare(request),request); env.enable();
  const result=env.api.prepare(request);
  assert.equal(JSON.stringify(request),original);
  assert.strictEqual(result.messages[0],request.messages[0]);
  assert.strictEqual(result.messages[1].content_blocks_v2[0],request.messages[1].content_blocks_v2[0]);
  const text=result.messages[1].content_blocks_v2[1].content.text_block.text;
  assert.equal(text, request.messages[1].content_blocks_v2[1].content.text_block.text+'\n\n'+env.api.rule);
  const p=JSON.parse(JSON.parse(result.messages[1].ext.chat_ability).ability_param);
  assert.equal(JSON.parse(p.input_box_content).user_input_content,text);
  assert.deepEqual({...p,input_box_content:null},{...params(),input_box_content:null});
  assert.strictEqual(env.api.prepare(result),result);
  env.enable(false);assert.strictEqual(env.api.prepare(request),request);
});
test('two-part rule keeps native originals and forbids cloud ffmpeg concatenation of marked page links',()=>{
  const env=environment();
  assert.match(env.api.rule,/禁止下载后用 ffmpeg/);
  assert.match(env.api.rule,/本地无水印助手按当前会话解析、排序/);
});
test('ordinary chat, assistant tail, non-video and other durations are untouched',()=>{
  const env=environment();env.enable();
  for(const request of [{messages:[{text:'聊天'}]}, {messages:[message(),{role:'assistant',text:'回复'}]},
    ...[4,15,60].map(duration=>({messages:[{...message(),ext:{chat_ability:{ability_type:17,ability_param:{...params(),duration}}}}]})),
    {messages:[{...message(),ext:{chat_ability:{ability_type:1,ability_param:params()}}}]}]) assert.strictEqual(env.api.prepare(request),request);
});
test('encoded wire messages and legacy skill preserve encoding and are idempotent',()=>{
  const env=environment();env.enable();
  const body=JSON.stringify([{block_type:10000,content:{text_block:{text:'剧本'}}},{block_type:2,content:{url:'ref'}}]);
  const wire={chat_ability:JSON.stringify({ability_type:17,ability_param:params()}),messages:JSON.stringify([{role:'user',content:body}])};
  const updated=env.api.prepare(wire);
  assert.equal(typeof updated.messages,'string');assert.match(JSON.parse(updated.messages)[0].content,/插件两段式/);
  assert.strictEqual(env.api.prepare(updated),updated);
  const legacy=[{body:'剧本',extraExt:{input_skill:JSON.stringify({skill_type:17,variables:JSON.stringify(params())})}}];
  const next=env.api.prepareLegacy(legacy);assert.equal(next[0].body,'剧本\n\n'+env.api.rule);
  assert.strictEqual(env.api.prepareLegacy(next),next);
});
test('pipeline and subsequent send hook never duplicate the rule',()=>{
  const env=environment();env.enable();
  const pipeline={content:{abilityType:17,abilityParams:params(),text:'剧本'}};
  const next=env.api.preparePipeline(pipeline);assert.equal(next.content.text,'剧本\n\n'+env.api.rule);
  assert.strictEqual(env.api.preparePipeline(next),next);
  const draft={messages:[{role:'user',text:next.content.text,ext:{chat_ability:{ability_type:17,ability_param:next.content.abilityParams}}}]};
  assert.strictEqual(env.api.prepare(draft),draft);
});
test('non-target sites do not install the module',()=>assert.equal(environment('example').api,undefined));
