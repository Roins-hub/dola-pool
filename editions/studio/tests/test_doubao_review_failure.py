"""Real review refusal wording must supersede stale accepted receipts."""
import copy
import unittest
from providers.doubao.runtime import receipt_state
from test_doubao_execution_evidence import ExecutionEvidenceTests, message, wire, CAP, ACK

REFUSALS=(
 '生成的视频可能涉及版权限制，请更换输入内容后重试',
 '生成的影片可能涉及版權限制，請更換輸入內容後重試',
 '视频未通过内容审核，请修改后重试',
)

class ReviewClassificationTests(unittest.TestCase):
 def test_observed_refusals_are_terminal(self):
  for text in REFUSALS:
   with self.subTest(text=text):self.assertEqual(receipt_state(text,CAP),'rejected')

 def test_pending_hypothetical_and_quoted_refusals_are_not_terminal(self):
  for text in ('视频审核中，请耐心等待','如果生成的视频可能涉及版权限制，请更换输入内容后重试',
               '你之前说“生成的影片可能涉及版權限制，請更換輸入內容後重試”，现在仍在生成中。',
               '该视频已通过内容审核，正在生成'):
   with self.subTest(text=text):self.assertNotEqual(receipt_state(text,CAP),'rejected')

class ReviewRuntimeTests(unittest.IsolatedAsyncioTestCase):
 asyncSetUp=ExecutionEvidenceTests.asyncSetUp
 observe=ExecutionEvidenceTests.observe
 async def test_later_review_failure_overrides_accepted_native_and_stale_dom(self):
  for text in REFUSALS:
   accepted=message(execution=True)
   refusal=message(text,mid='refusal');refusal['index_in_conv']=17
   result=await self.observe(accepted,refusal)
   self.assertEqual(result['phase'],'rejected')
   self.assertFalse(result['no_charge'])
   self.assertEqual(result['error_code'],'platform_review_rejected')
   self.assertIn(text,result['platform_reply'])
   self.assertTrue(result['generation_evidence']['confirmed'])
   self.assertEqual(await self.page.locator('body').get_attribute('data-sent'),'0')

 async def test_newer_actual_generation_supersedes_old_refusal(self):
  refusal=message(REFUSALS[0],mid='refusal');refusal['index_in_conv']=3
  accepted=message(execution=True);accepted['index_in_conv']=8
  result=await self.observe(refusal,accepted)
  self.assertEqual(result['phase'],'accepted')

 async def test_foreign_baseline_user_and_unfinished_refusal_cannot_end_task(self):
  for changes in ({'conversation_id':'other'},{'message_id':'old'},{'user_type':1},{'ext':{'is_finish':'0'}}):
   self.snapshot['baseline_messages']=['old']
   accepted=message(execution=True)
   refusal=message(REFUSALS[0],mid='refusal');refusal['index_in_conv']=17;refusal.update(changes)
   result=await self.observe(accepted,refusal)
   self.assertEqual(result['phase'],'accepted')

 async def test_thirty_second_review_failure_has_same_behavior(self):
  self.snapshot['request'].update(model='dialog_seedance_2.5',duration=30)
  accepted=message('本次使用 Seedance 2.5 生成，预计等待15分钟。',execution=True)
  accepted['ext']['chat_ability']='{}'
  refusal=message(REFUSALS[1],mid='refusal');refusal['index_in_conv']=17
  self.session.payloads=[wire(accepted,refusal)]
  result=await self.session.observe(self.snapshot,{**copy.deepcopy(CAP),'id':'dialog_seedance_2.5'},timeout=0)
  self.assertEqual(result['phase'],'rejected')

class ReviewSettlementTests(unittest.IsolatedAsyncioTestCase):
 async def test_review_failure_releases_inflight_but_does_not_invent_a_refund(self):
  import tempfile
  from pathlib import Path
  from providers.doubao import DoubaoProvider
  from test_doubao_provider import ck, PROXY
  with tempfile.TemporaryDirectory() as root:
   p=DoubaoProvider(root=Path(root),enabled=True)
   self.addAsyncCleanup(p.shutdown)
   p.import_ck('fixture',ck(),PROXY)
   for index,no_charge in enumerate((False,True)):
    task='video_'+str(index)*32
    p.accounts.reserve(task,'fixture')
    snap={'id':task,'account':'fixture','submitted_duration':30,'request':{'duration':30}}
    result=await p._finish(None,snap,{'phase':'rejected','error_code':'platform_review_rejected',
      'platform_reply':REFUSALS[0],'no_charge':no_charge})
    self.assertEqual(result['status'],'failed')
    self.assertEqual(result['phase'],'platform_rejected_no_charge' if no_charge else 'platform_review_rejected')
    account=p.list_accounts()[0]
    self.assertEqual(account['inflight_tasks'],0)
    self.assertEqual(account['quota_used'],0)
    self.assertEqual(account['quota_reserved'],1)

if __name__=='__main__':unittest.main()
