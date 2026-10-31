const test = require('node:test');
const assert = require('node:assert/strict');
const { audit } = require('./audit-two-part-protocol.cjs');
test('audit keeps unknown and incomplete evidence unverified, without exporting private values', () => {
  const result = audit({debuggerNetwork:{requests:[{kind:'submission',id:1,status:200,request:{comparison:{fieldDetails:{complete:false,fields:[
    {path:'ability.video_count',type:'number',value:2},
    {path:'option.server_do_compare',type:'boolean',value:true},
    {path:'ext.chat_history_count',type:'string',value:'private'},
    {path:'clientMeta.conversation_id',type:'string',value:'private-id'}]}}}}]}});
  assert.equal(result[0].fieldCoverageComplete,false);
  assert.equal(result[0].candidates.length,3);
  assert.match(result[0].verdict,/UNVERIFIED/);
  assert.doesNotMatch(JSON.stringify(result),/private/);
  assert.deepEqual(audit({}),[]);
});
