import base64
import json
import unittest

from video_polling import parse_poll_response


def response(messages, conversation_id='123'):
    return {'status_code': 0, 'downlink_body': {'pull_singe_chain_downlink_body':
            {'conversation_id': conversation_id, 'messages': messages}}}


def message(text=None, video=None, conversation_id='123', role='assistant'):
    blocks = []
    if text:
        blocks.append({'content': {'text_block': {'text': text}}})
    if video:
        blocks.append({'block_type': 2074, 'content': {'creation_block': {'creations': [
            {'id': 'video-new', 'type': 2, 'video': video}]}}})
    return {'role': role, 'conversation_id': conversation_id, 'content': json.dumps(blocks)}


class AssociationTests(unittest.TestCase):
    def test_user_echo_and_other_conversation_are_not_receipts_or_results(self):
        poll = parse_poll_response(response([
            message('Seedance 2.5 消耗2个额度', role='user'),
            message('wrong', {'download_url': 'https://cdn.test/old.mp4'}, conversation_id='999'),
            message('真实回复')]), conversation_id='123')
        self.assertEqual(poll['assistant_texts'], ['真实回复'])
        self.assertEqual(poll['videos'], [])

    def test_model_only_video_is_extracted_and_seen_media_is_excluded(self):
        model = json.dumps({'video_list': {'v': {'main_url': base64.b64encode(b'https://cdn.test/new.mp4?sign=private').decode(),
                                                'bitrate': 1000}}})
        data = response([message(video={'video_model': model})])
        poll = parse_poll_response(data, conversation_id='123')
        self.assertEqual(len(poll['videos']), 1)
        self.assertEqual(len(poll['videoModels']), 1)
        self.assertEqual(parse_poll_response(data, conversation_id='123', baseline_media=poll['mediaKeys'])['videos'], [])

    def test_a_wrong_top_level_conversation_is_rejected(self):
        with self.assertRaises(RuntimeError):
            parse_poll_response(response([], conversation_id='999'), conversation_id='123')
