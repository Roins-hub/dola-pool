"""Validate the public schema and persistent intent, without importing live service state."""
import ast
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace
import unittest

from fastapi import HTTPException
from pydantic import BaseModel, Field, model_validator, ValidationError


def contracts():
    tree = ast.parse(Path('server.py').read_text(encoding='utf-8'))
    names = {'VideoGenRequest', 'TaskResponse', '_submission_identity', '_plugin_task_options'}
    nodes = [n for n in tree.body if isinstance(n, (ast.ClassDef, ast.FunctionDef)) and n.name in names]
    ns = {'BaseModel': BaseModel, 'Field': Field, 'model_validator': model_validator,
        'config': SimpleNamespace(DEFAULT_MODEL='seedance-2.0'), 'HTTPException': HTTPException,
        'hashlib': hashlib, 'json': json, '_resolve_ratio': lambda size, ratio: ratio}
    exec(compile(ast.fix_missing_locations(ast.Module(body=nodes,type_ignores=[])), 'real-api-contracts', 'exec'), ns)
    return ns


class Expert30ApiTests(unittest.TestCase):
    def setUp(self):
        self.ns = contracts()

    def test_new_profile_supplies_its_own_defaults_and_accepts_480p(self):
        Req = self.ns['VideoGenRequest']
        req = Req(prompt='原始提示词', duration_profile='plugin-expert30', resolution='480p')
        self.assertEqual((req.model,req.duration,req.ratio,req.resolution), ('seedance-2.5',30,'16:9','480p'))

    def test_batch_count_is_strict_and_bounded(self):
        Req = self.ns['VideoGenRequest']
        for count in (0,21,1.5,'2',True):
            with self.subTest(count=count), self.assertRaises(ValidationError):
                Req(prompt='test',batch_count=count)
        self.assertEqual(Req(prompt='test',batch_count=20).batch_count,20)

    def test_two_part_is_explicit_strict_and_only_allowed_for_thirty_second_plugin_requests(self):
        Req = self.ns['VideoGenRequest']
        self.assertFalse(getattr(Req(prompt='test', duration_profile='plugin-expert30'), 'two_part', True))
        self.assertTrue(Req(prompt='test', duration_profile='plugin-expert30', two_part=True).two_part)
        for values in ({'two_part':'true'}, {'two_part':1}, {'two_part':True, 'duration':15}):
            with self.subTest(values=values), self.assertRaises(ValidationError):
                Req(prompt='test', duration_profile='plugin-expert30', **values)
        with self.assertRaises(ValidationError):
            Req(prompt='test', two_part=True)

    def test_new_profile_identity_includes_resolution_and_batch_count(self):
        Req = self.ns['VideoGenRequest']
        values = {'prompt':'exact', 'duration_profile':'plugin-expert30', 'idempotency_key':'one-intent-001'}
        identity = self.ns['_submission_identity']
        a = identity(Req(**values,resolution='720p',batch_count=2),'member')
        b = identity(Req(**values,resolution='1080p',batch_count=2),'member')
        c = identity(Req(**values,resolution='720p',batch_count=3),'member')
        d = identity(Req(**values,resolution='720p',batch_count=2,two_part=True),'member')
        self.assertEqual(len({a['request_fingerprint'],b['request_fingerprint'],c['request_fingerprint'],d['request_fingerprint']}),4)

    def test_recovery_options_reconstruct_requested_duration_for_legacy_tasks(self):
        from store import TaskStore
        store = TaskStore(':memory:')
        self.addCleanup(store._conn.close)
        self.ns['store'] = store
        for duration in (5, 10, 15, 30):
            row = {'id':'legacy', 'duration':duration, 'generation_options':'{"resolution":"720p"}'}
            with self.subTest(duration=duration):
                options = self.ns['_plugin_task_options'](row)
                self.assertEqual(options.get('requested_duration'), duration)
                self.assertFalse(options.get('two_part', False))

    def test_default_expert_request_preserves_existing_idempotency_fingerprint(self):
        Req = self.ns['VideoGenRequest']
        request = Req(prompt='exact', duration_profile='plugin-expert30', idempotency_key='legacy-intent-001')
        identity = self.ns['_submission_identity'](request, 'member')
        self.assertEqual(identity['request_fingerprint'],
                         'd044410b8786a12797c51beb9ed08626ffc7dd05a7f42bbe26e798fed3f4fef9')

    def test_response_keeps_all_clips_and_does_not_collapse_to_first(self):
        row = self.ns['TaskResponse'](id='fixture',status='processing',clips=[
            {'clip_id':'one','status':'completed','video_url':'/videos/one.mp4'},
            {'clip_id':'two','status':'processing'}],total_actual_duration=15,batch_id='batch-1',batch_index=2)
        value = row.model_dump()
        self.assertEqual(len(value.get('clips',[])),2)
        self.assertEqual(value.get('batch_index'),2)

    def test_capabilities_publish_profile_and_filter_it_by_member_duration_permission(self):
        from platform_runtime import dola_capabilities, filtered_capabilities
        caps = dola_capabilities()
        profiles = caps.get('profiles') or []
        self.assertEqual(len(profiles),1)
        self.assertEqual(profiles[0]['id'],'plugin-expert30')
        self.assertEqual(len(profiles[0]['models']),4)
        profile = profiles[0]
        self.assertEqual(profile['label'], '专家模式')
        self.assertEqual(profile['references'], {'images':True, 'audio':True, 'video':True})
        for model in profile['models']:
            self.assertEqual(model['durations'], [5,10,15,30])
        allowed = filtered_capabilities(caps, {'allowed_platforms':['dola'],'allowed_durations':[10]})
        self.assertTrue(allowed['profiles'][0]['enabled'])
        self.assertTrue(all(m['durations']==[10] for m in allowed['profiles'][0]['models']))
        denied = filtered_capabilities(caps, {'allowed_platforms':['dola'],'allowed_durations':[20]})
        self.assertFalse(denied['profiles'][0]['enabled'])
