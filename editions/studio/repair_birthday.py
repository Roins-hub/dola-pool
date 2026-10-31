"""Complete an explicitly supplied real birthday for paused Dola accounts.

Usage: python repair_birthday.py --birthday YYYY-MM-DD account1 [account2 ...]
Never submits a chat/video request or guesses a date. The birthday is not logged.
"""
import argparse
import asyncio
import contextlib
from datetime import date
import io
import json
from pathlib import Path
import re
import sqlite3

from patchright.async_api import TimeoutError as BrowserTimeout


BIRTHDAY = re.compile(r'Geburtstag|Geburtsdatum|date of birth|birthday|生日|出生日期', re.I)
DATE_INPUT = 'input[type="date"], input[placeholder*="yyyy" i]'
CONTINUE = re.compile(r'^(Weiter|Continue|继续)$', re.I)
SIGN_IN = re.compile(r'^(Anmelden|Sign in|Log in|登录|登入|Mit Google fortfahren|Continue with Google)$', re.I)
PAGE_ERROR = re.compile(r'^(This page is temporarily unavailable|该页面暂时不可用)$', re.I)


def validate_birthday(value):
    parsed = date.fromisoformat(value)
    if parsed.isoformat() != value or parsed > date.today():
        raise ValueError('请提供真实出生日期，格式为 YYYY-MM-DD')
    return value


async def first_visible(locator):
    for i in range(await locator.count()):
        item = locator.nth(i)
        if await item.is_visible():
            return item
    return None


async def birthday_input(page):
    if await first_visible(page.get_by_text(BIRTHDAY)) is None:
        return None
    return await first_visible(page.locator(DATE_INPUT))


async def page_state(page):
    if await birthday_input(page) is not None:
        return 'birthday_required'
    if await first_visible(page.get_by_text(PAGE_ERROR)) is not None:
        return 'page_unavailable'
    if await first_visible(page.get_by_role('button', name=SIGN_IN)) is not None:
        return 'login_required'
    if await first_visible(page.locator('textarea,[contenteditable="true"]')) is not None:
        return 'chat_ready'
    return 'loading'


async def submit_birthday(page, birthday, timeout_ms=20000):
    validate_birthday(birthday)
    field = await birthday_input(page)
    if field is None:
        return 'not_shown'
    if not await field.is_editable():
        return 'date_picker_required'
    await field.fill(birthday, timeout=5000)
    await field.press('Tab')
    if await field.input_value() != birthday:
        return 'date_not_accepted'
    button = await first_visible(page.get_by_role('button', name=CONTINUE))
    if button is None or not await button.is_enabled():
        return 'continue_disabled'
    await button.click(timeout=5000)
    try:
        await field.wait_for(state='hidden', timeout=timeout_ms)
    except BrowserTimeout:
        return 'still_pending'
    return 'submitted'


def restore_scheduling(database, account, result):
    if result != 'verified':
        return False
    with contextlib.closing(sqlite3.connect(database)) as conn, conn:
        changed = conn.execute(
            "UPDATE accounts_meta SET scheduling=1,verification_required='' "
            "WHERE name=? AND scheduling=0 AND verification_required='birthday'", (account,)).rowcount
    return changed == 1


async def wait_for_page(page, seconds=30):
    for _ in range(seconds):
        state = await page_state(page)
        if state != 'loading':
            return state
        await page.wait_for_timeout(1000)
    return 'loading'


async def repair_account(playwright, account, birthday):
    from browser import cookie_value, launch_account_context
    from video_worker_ui import _dismiss_cookie_banner
    with contextlib.redirect_stdout(io.StringIO()):
        context = await launch_account_context(playwright, account, headless=False)
    try:
        page = context.pages[0] if context.pages else await context.new_page()
        state = 'loading'
        for attempt in range(3):
            try:
                await page.goto('https://www.dola.com/chat', wait_until='domcontentloaded', timeout=60000)
                state = await wait_for_page(page)
                if state not in ('loading', 'page_unavailable'):
                    break
            except Exception as exc:
                print(f'[{account}] 页面打开失败，重试 {attempt + 1}/3 ({type(exc).__name__})', flush=True)
            await page.wait_for_timeout(2000)
        await _dismiss_cookie_banner(page)
        # Give deferred overlays a chance to render, without sending any message.
        await page.wait_for_timeout(3000)
        result = await submit_birthday(page, birthday)
        if result != 'submitted':
            state = await page_state(page)
            return state if result == 'not_shown' else result
        print(f'[{account}] 生日表单已提交，重新加载核对结果', flush=True)
        await page.reload(wait_until='domcontentloaded', timeout=60000)
        state = await wait_for_page(page)
        await page.wait_for_timeout(5000)
        state = await page_state(page)
        cookies = await context.cookies('https://www.dola.com')
        if state != 'chat_ready' or not cookie_value(cookies, 'sessionid'):
            return state if state != 'chat_ready' else 'login_required'
        # Keep renewed login state in the existing ignored profile directory.
        path = Path('accounts') / account / 'cookies.json'
        temporary = path.with_suffix('.birthday.tmp')
        temporary.write_text(json.dumps(cookies), encoding='utf-8')
        temporary.replace(path)
        return 'verified'
    finally:
        await context.close()


async def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--birthday', required=True, type=validate_birthday)
    parser.add_argument('accounts', nargs='+')
    args = parser.parse_args()
    from patchright.async_api import async_playwright
    labels = {
        'verified': '生日验证通过，已恢复调度',
        'birthday_required': '生日验证仍未通过，保持暂停',
        'login_required': '页面要求重新登录，保持暂停',
        'chat_ready': '未出现生日表单，未提交生日，保持暂停',
        'page_unavailable': 'Dola 页面暂时不可用，保持暂停',
        'loading': '页面未加载完成，保持暂停',
        'still_pending': '提交后生日框仍在，保持暂停',
    }
    async with async_playwright() as p:
        for account in dict.fromkeys(args.accounts):
            if not re.fullmatch(r'[A-Za-z0-9_-]+', account):
                raise ValueError('账号名格式错误')
            with contextlib.closing(sqlite3.connect('pool_usage.db')) as db:
                row = db.execute('select scheduling,verification_required from accounts_meta where name=?', (account,)).fetchone()
            with contextlib.closing(sqlite3.connect('tasks.db')) as db:
                busy = db.execute("select count(*) from tasks where account=? and status in ('queued','processing')", (account,)).fetchone()[0]
            if row != (0, 'birthday') or busy:
                print(f'[{account}] 账号未处于暂停验证状态或正在运行，跳过', flush=True)
                continue
            print(f'[{account}] 开始处理生日验证', flush=True)
            try:
                result = await repair_account(p, account, args.birthday)
            except Exception as exc:
                result = type(exc).__name__
            restored = restore_scheduling('pool_usage.db', account, result)
            print(json.dumps({'account': account, 'result': result, 'scheduling_restored': restored,
                              'message': labels.get(result, '未确认成功，保持暂停')}, ensure_ascii=False), flush=True)


if __name__ == '__main__':
    asyncio.run(main())
