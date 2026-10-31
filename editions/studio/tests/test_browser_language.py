import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import browser


class BrowserLanguageTests(unittest.TestCase):
    def test_existing_profile_language_is_aligned_without_erasing_other_preferences(self):
        self.assertTrue(hasattr(browser, 'prepare_profile_language'))
        with tempfile.TemporaryDirectory() as root:
            profile = Path(root)
            (profile/'Default').mkdir()
            pref = profile/'Default'/'Preferences'
            pref.write_text(json.dumps({'intl':{'selected_languages':'zh-CN,zh'},
                'translate':{'enabled':True}, 'unrelated':{'retained':True}}))
            with patch.object(browser.config, 'BROWSER_LOCALE','de-DE'), patch.object(browser.config, 'BROWSER_ACCEPT_LANGUAGE','de-DE,de;q=0.9,en;q=0.8'):
                browser.prepare_profile_language(profile)
            data=json.loads(pref.read_text())
            self.assertEqual(data['intl']['selected_languages'],'de-DE,de,en')
            self.assertEqual(data['intl']['accept_languages'],'de-DE,de,en')
            self.assertFalse(data['translate']['enabled'])
            self.assertEqual(data['unrelated'],{'retained':True})

    def test_corrupt_preferences_are_not_overwritten(self):
        self.assertTrue(hasattr(browser, 'prepare_profile_language'))
        with tempfile.TemporaryDirectory() as root:
            p=Path(root)/'Default';p.mkdir()
            f=p/'Preferences';f.write_text('invalid profile data')
            browser.prepare_profile_language(Path(root))
            self.assertEqual(f.read_text(),'invalid profile data')
