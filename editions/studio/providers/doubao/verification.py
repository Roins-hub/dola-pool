"""Read-only detection and bounded handoff to a human in the original browser."""
import re


async def challenge_visible(page):
    # Container names come from the site's captured VerifyCenter SDK. Text in
    # a chat message and a hidden, preloaded widget are not a live challenge.
    for frame in page.frames:
        try:
            if frame != page.main_frame and not await (await frame.frame_element()).is_visible():
                continue
            detected = await frame.evaluate('''()=>{
          const visible=n=>{const s=getComputedStyle(n),r=n.getBoundingClientRect();
            return s.display!=='none'&&s.visibility!=='hidden'&&s.opacity!=='0'&&r.width>0&&r.height>0};
          return [...document.querySelectorAll('#captcha_container,#vc_captcha_wrapper,#captcha-container-fullscreen')]
            .some(n=>visible(n)&&[...n.querySelectorAll('iframe,img,canvas,input,button')].some(visible));
            }''')
        except Exception:
            # The SDK removes/replaces its iframe after a human completes it.
            # That normal transition must not be mistaken for a closed browser.
            if frame.is_detached() and not page.is_closed():
                continue
            raise
        if detected:
            return True
    return False


async def original_request_visible(page, snapshot, capability):
    """Never attribute a separately typed request to the fenced submission."""
    prompt = snapshot.get('submission_prompt')
    if not prompt:
        return False
    texts = await page.locator('[data-message-id]').evaluate_all('''(nodes,assistant)=>nodes
      .filter(n=>!n.matches(assistant)&&!n.closest(assistant)&&!n.querySelector(assistant))
      .filter(n=>!n.parentElement?.closest('[data-message-id]'))
      .map(n=>n.textContent||'')''', capability['selectors']['assistant'])
    normalize = lambda text: re.sub(r'\s+', '', text)
    expected = normalize(prompt)
    # A challenge can be raised by the fenced confirmation instead of the
    # original send. Only our already-recorded follow-ups may trail that
    # original request; a separately typed request must still fail the check.
    from .confirmation import CONFIRM_TEXT, NUDGE_TEXT
    allowed = set()
    if snapshot.get('confirmation_sent'):
        allowed.update((normalize(CONFIRM_TEXT), normalize('确定生成')))
    if snapshot.get('nudge_sent'):
        allowed.add(normalize(NUDGE_TEXT))
    for text in reversed(texts):
        observed = normalize(text)
        if expected in observed:
            return True
        if observed not in allowed:
            return False
    return False


def incomplete(snapshot, reason):
    return {'phase': 'verification_incomplete', 'conversation_id': snapshot.get('conversation_id'),
            'page_url': snapshot.get('page_url') or snapshot.get('submission_page_url'),
            'media': [], 'platform_reply': snapshot.get('platform_reply', ''),
            'no_charge': False, 'error_code': 'verification_incomplete', 'reason': reason,
            'verification_pending': False}
