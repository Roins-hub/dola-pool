import importlib.util
from contextlib import closing
from pathlib import Path
import tempfile
import sqlite3
import unittest

from patchright.async_api import async_playwright


class BirthdayRepairTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        path = Path(__file__).resolve().parents[1] / 'repair_birthday.py'
        self.assertTrue(path.exists(), 'missing birthday repair program')
        spec = importlib.util.spec_from_file_location('birthday_repair_test', path)
        self.repair = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.repair)
        self.p = await async_playwright().start()
        self.browser = await self.p.chromium.launch(headless=True)
        self.page = await self.browser.new_page()

    async def asyncTearDown(self):
        if hasattr(self, 'browser'):
            await self.browser.close()
            await self.p.stop()

    async def test_submits_exact_supplied_date_once_and_waits_for_form_to_close(self):
        await self.page.set_content('''<div id="gate"><h2>Wann hast du Geburtstag?</h2>
          <input type="date"><button onclick="document.body.dataset.sent=document.querySelector('input').value;
          document.body.dataset.calls=Number(document.body.dataset.calls||0)+1;document.querySelector('#gate').remove()">Weiter</button></div>''')
        self.assertEqual(await self.repair.submit_birthday(self.page, '2000-02-29', timeout_ms=100), 'submitted')
        self.assertEqual(await self.page.locator('body').get_attribute('data-sent'), '2000-02-29')
        self.assertEqual(await self.page.locator('body').get_attribute('data-calls'), '1')

    async def test_visible_form_after_click_is_not_reported_as_success(self):
        await self.page.set_content('<h2>Wann hast du Geburtstag?</h2><input placeholder="YYYY-MM-DD"><button>Weiter</button>')
        self.assertEqual(await self.repair.submit_birthday(self.page, '2000-02-29', timeout_ms=100), 'still_pending')

    async def test_invalid_date_is_rejected_before_any_form_action(self):
        await self.page.set_content('<h2>Wann hast du Geburtstag?</h2><input type="date"><button>Weiter</button>')
        with self.assertRaises(ValueError):
            await self.repair.submit_birthday(self.page, '2001-02-29')
        self.assertEqual(await self.page.locator('input').input_value(), '')

    async def test_login_page_is_not_verification_success(self):
        await self.page.set_content('<button>Anmelden</button><button>Mit Google fortfahren</button><textarea></textarea>')
        self.assertEqual(await self.repair.submit_birthday(self.page, '2000-02-29'), 'not_shown')
        self.assertEqual(await self.repair.page_state(self.page), 'login_required')

    async def test_network_error_with_no_form_is_not_verification_success(self):
        await self.page.set_content('<h2>This page is temporarily unavailable</h2><button>Refresh</button>')
        self.assertEqual(await self.repair.page_state(self.page), 'page_unavailable')

    async def test_only_confirmed_success_restores_scheduler(self):
        with tempfile.TemporaryDirectory() as root:
            db = Path(root) / 'pool.db'
            with closing(sqlite3.connect(db)) as c, c:
                c.execute('create table accounts_meta(name text,scheduling integer,verification_required text)')
                c.execute("insert into accounts_meta values ('fixture',0,'birthday')")
            self.assertFalse(self.repair.restore_scheduling(db, 'fixture', 'login_required'))
            with closing(sqlite3.connect(db)) as c:
                self.assertEqual(c.execute('select scheduling from accounts_meta').fetchone()[0], 0)
            self.assertTrue(self.repair.restore_scheduling(db, 'fixture', 'verified'))
            with closing(sqlite3.connect(db)) as c:
                self.assertEqual(c.execute('select scheduling,verification_required from accounts_meta').fetchone(), (1, ''))


if __name__ == '__main__':
    unittest.main()
