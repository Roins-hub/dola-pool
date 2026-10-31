"""Restart and cache policy regressions; only reusable public bytes reach disk."""
import tempfile
import unittest
from pathlib import Path

import public_assets

BASE='https://sf-flow-web-cdn.ciciai.com/obj/ocean-flow-web-sg/dola_web/static/'
URL=BASE+'js/panel.abcdef12.js'


class DiskCacheTests(unittest.TestCase):
    def cache(self, root, now, size=1024):
        self.assertTrue(hasattr(public_assets, 'PersistentAssetCache'), 'Public cache currently disappears on restart')
        return public_assets.PersistentAssetCache(Path(root)/'assets.db', max_bytes=size, clock=lambda:now[0])

    def test_restart_preserves_bytes_but_does_not_reset_expiry(self):
        with tempfile.TemporaryDirectory() as root:
            now=[1000.0]
            first=self.cache(root,now)
            first.put(URL,b'public script',{'Cache-Control':'public,max-age=60','Age':'50'},object())
            first.close()
            now[0]+=5
            second=self.cache(root,now)
            self.assertEqual(second.get(URL,object())[0],b'public script')
            second.close()
            now[0]+=6
            third=self.cache(root,now)
            self.assertIsNone(third.get(URL,object()))
            third.close()

    def test_sensitive_dynamic_and_varying_responses_never_survive_restart(self):
        variants=[(URL,{'Cache-Control':'private,max-age=60'}),
                  (URL,{'Cache-Control':'no-store,max-age=60'}),
                  (URL,{'Cache-Control':'no-cache,max-age=60'}),
                  (URL,{'Cache-Control':'public,max-age=60','Set-Cookie':'secret'}),
                  (URL,{'Cache-Control':'public,max-age=60','Vary':'Cookie'}),
                  (URL+'?token=secret',{'Cache-Control':'public,max-age=60'}),
                  (BASE+'js/plain.js',{'Cache-Control':'public,max-age=60'}),
                  ('https://starling-oversea.byteoversea.com'+public_assets.LANGUAGE_PATH+'?lang=en',
                   {'Cache-Control':'public,max-age=60'})]
        for url,headers in variants:
            with self.subTest(url=url,headers=headers), tempfile.TemporaryDirectory() as root:
                now=[1000.0]
                first=self.cache(root,now)
                first.put(url,b'account data',headers,object())
                first.close()
                second=self.cache(root,now)
                self.assertIsNone(second.get(url,object()))
                second.close()

    def test_disk_capacity_evicts_old_data_across_process_instances(self):
        with tempfile.TemporaryDirectory() as root:
            now=[1000.0]
            first=self.cache(root,now,15)
            first.put(URL,b'1234567890',{'Cache-Control':'public,max-age=60'},object())
            now[0]+=1
            other=BASE+'js/other.11223344.js'
            first.put(other,b'abcdefghij',{'Cache-Control':'public,max-age=60'},object())
            first.close()
            second=self.cache(root,now,15)
            self.assertIsNone(second.get(URL,object()))
            self.assertEqual(second.get(other,object())[0],b'abcdefghij')
            second.close()

    def test_startup_preloads_valid_public_bytes_without_extending_lifetime(self):
        with tempfile.TemporaryDirectory() as root:
            now=[1000.0]
            first=self.cache(root,now)
            first.put(URL,b'script',{'Cache-Control':'public,max-age=60'},object())
            first.close()
            second=self.cache(root,now)
            try:
                self.assertTrue(hasattr(second,'preload'), 'Startup cannot yet prepare reusable disk resources')
                self.assertEqual(second.preload(),1)
                now[0]+=61
                self.assertIsNone(second.get(URL,object()))
            finally:
                second.close()
