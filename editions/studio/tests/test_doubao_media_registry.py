import unittest
from providers.doubao.media_registry import VideoRegistry, url_key


class VideoRegistryTests(unittest.TestCase):
    def test_signed_url_changes_keep_identity_but_business_ids_are_distinct(self):
        self.assertEqual(url_key('https://cdn.example/v.mp4?id=1&sign=a&expires=1'),url_key('https://cdn.example/v.mp4?expires=2&id=1&sign=b'))
        self.assertNotEqual(url_key('https://cdn.example/v.mp4?id=1'),url_key('https://cdn.example/v.mp4?id=2'))

    def test_old_node_on_another_cdn_is_not_a_new_video(self):
        registry=VideoRegistry('chat',records=[{'conversation_id':'chat','message_id':'old','node_id':'7000000000000000002','url':'https://old.example/a.mp4'}])
        self.assertFalse(registry.is_new({'conversation_id':'chat','message_id':'new','node_id':'7000000000000000002','url':'https://new.example/a.mp4'}))
        self.assertTrue(registry.is_new({'conversation_id':'chat','message_id':'new','node_id':'7000000000000000006','url':'https://new.example/a.mp4'}))

    def test_fallback_identity_and_baseline_keys_survive_reconstruction(self):
        record={'conversation_id':'chat','message_id':'a','fallback_api':'https://cdn.example/resolve?id=7&sign=a','url':'https://cdn.example/a.mp4'}
        registry=VideoRegistry('chat',records=[record])
        restored=VideoRegistry('chat',keys=registry.keys())
        self.assertFalse(restored.is_new({**record,'url':'https://else.example/b.mp4','fallback_api':'https://cdn.example/resolve?sign=b&id=7'}))

    def test_wrong_chat_or_missing_association_is_never_accepted(self):
        registry=VideoRegistry('chat')
        for record in ({'conversation_id':'other','message_id':'1','url':'https://cdn.example/a.mp4'},
                       {'conversation_id':'chat','url':'https://cdn.example/a.mp4'}):
            self.assertFalse(registry.is_new(record))


if __name__=='__main__':unittest.main()
