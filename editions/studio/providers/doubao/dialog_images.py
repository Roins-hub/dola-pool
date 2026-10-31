"""Read-only image attachment checks for Doubao's ordinary dialog uploader.

The caller supplies observed native selectors and authorized local references.
This adapter never changes native upload types, constructs a request, signs a
request, or clicks Send. A completed thumbnail alone is not upload evidence.
"""
import asyncio
import logging
import math
import re
import time

from providers.base import ProviderError
from .materials import attach_native, normalize_references


class _AttachmentStatePending(ProviderError):
    """A mounted thumbnail has not yet provided a stable native owner state."""


_CARD = '''element=>{
  const records=[];
  const key=element.getAttribute('data-attachment-key');
  const name=element.getAttribute('data-file-name');
  const type=element.getAttribute('data-attachment-type')||element.getAttribute('data-kind');
  if(key!==null||name!==null||type!==null)records.push({key,name,type});
  const seen=new WeakSet();
  function visit(value,depth=0){
    if(!value||typeof value!=='object'||depth>3||seen.has(value))return;
    seen.add(value);
    const key=value.attachmentKey??value.attachment_key;
    if(key!==undefined){
      let type=value.type??value.kind;
      if(type===1)type='image';
      records.push({key,name:value.name??value.fileName??value.file?.name,type});
    }
    for(const field of ['attachment','attachmentState','reference'])visit(value[field],depth+1);
  }
  // This exact card's props only: no thread/sibling/global attachment lookup.
  for(const field of Object.keys(element))if(field.startsWith('__reactProps$'))visit(element[field]);
  return records;
}'''


_NATIVE_CARD = '''element=>{
  const states=[];
  const key=Object.keys(element).find(key=>key.startsWith('__reactFiber$'));
  function committed(fiber){
    if(!fiber||!fiber.alternate)return fiber;
    const alternate=fiber.alternate;
    let left=fiber,right=alternate;
    // Resolve the committed branch by pointer identity. Never choose a branch
    // because its attachment happens to look completed, or read sibling data.
    for(let depth=0;depth<80;depth++){
      const parent=left.return;
      if(!parent){
        const current=left.stateNode?.current;
        return current===left?fiber:current===right?alternate:null;
      }
      const other=parent.alternate;
      if(!other){
        if(!parent.return)return null;
        left=right=parent.return;
        continue;
      }
      if(parent.child&&parent.child===other.child){
        for(let child=parent.child,count=0;child&&count<256;child=child.sibling,count++){
          if(child===left)return fiber;
          if(child===right)return alternate;
        }
        return null;
      }
      if(left.return!==right.return){left=parent;right=other;}
      else{
        let found=false;
        for(const [owner,peer] of [[parent,other],[other,parent]]){
          for(let child=owner.child,count=0;child&&count<256;child=child.sibling,count++){
            if(child===left){left=owner;right=peer;found=true;break;}
            if(child===right){right=owner;left=peer;found=true;break;}
          }
          if(found)break;
        }
        if(!found)return null;
      }
      if(left.alternate!==right)return null;
    }
    return null;
  }
  const current=committed(key&&element[key]);
  for(let fiber=current,hop=0;fiber&&hop<=3;fiber=committed(fiber.return),hop++){
    const host=fiber.stateNode;
    if(host instanceof Element&&host!==element&&typeof host.className==='string'
       &&host.className.includes('carousel-item'))break;
    const state=fiber.memoizedProps?.attachmentState;
    if(!state||typeof state!=='object')continue;
    states.push({type:state.type,name:state.fileName,key:state.fileKey,
      local_key:state.localKey,size:state.size,status:state.status,percent:state.uploadPercent,
      image_keys:Array.isArray(state.imageList)?state.imageList.map(image=>image?.key):null,
      width:state.imageWidth,height:state.imageHeight});
  }
  return {name:element.getAttribute('aria-label'),type:element.getAttribute('data-kind'),states};
}'''


def _native(capability):
    return capability.get('references', {}).get('images', {}).get('native_state') == 'attachment_state_v1'


def _selectors(capability):
    selectors = capability.get('selectors', {})
    if _native(capability):
        if any(not isinstance(selectors.get(key), str) or not selectors[key].strip()
               for key in ('upload_image', 'upload_ready')):
            raise ProviderError('unsupported_reference', '豆包普通对话的图片原生输入及附件卡尚未识别；提示词未发送。')
        return selectors
    if any(not isinstance(selectors.get(key), str) or not selectors[key].strip()
           for key in ('upload_ready', 'upload_pending', 'upload_error')):
        raise ProviderError('unsupported_reference', '豆包普通对话的图片上传完成、等待或失败状态尚未识别；提示词未发送。')
    direct = selectors.get('upload_image') or selectors.get('upload')
    chooser = selectors.get('upload_trigger') and selectors.get('upload_option')
    if not direct and not chooser:
        raise ProviderError('unsupported_reference', '豆包普通对话的原生图片上传入口尚未识别；提示词未发送。')
    return selectors


async def _cards(page, selectors):
    if (await page.locator(selectors['upload_pending']).count()
            or await page.locator(selectors['upload_error']).count()):
        raise ProviderError('reference_upload_failed', '豆包原生图片仍在上传或已失败；提示词未发送。', 409)
    result = []
    ready = page.locator(selectors['upload_ready'])
    for index in range(await ready.count()):
        card = ready.nth(index)
        if not await card.is_visible():
            raise ProviderError('reference_upload_unverified', '豆包原生图片完成卡不可见，未确认参考图片。', 409)
        records = await card.evaluate(_CARD, isolated_context=False)
        identities = set()
        for record in records:
            key, name, kind = record.get('key'), record.get('name'), record.get('type')
            if (not isinstance(key, str) or not 0 < len(key) <= 512 or any(ord(c) < 32 for c in key)
                    or not isinstance(name, str) or not 0 < len(name) <= 255
                    or re.search(r'[/\\\x00-\x1f]', name) or kind != 'image'):
                raise ProviderError('reference_upload_unverified', '豆包未明确确认原生图片的类型、名称和引用标识；提示词未发送。', 409)
            identities.add((key, name, kind))
        if len(identities) != 1:
            raise ProviderError('reference_upload_unverified', '豆包原生图片引用缺失或不唯一；提示词未发送。', 409)
        result.append(next(iter(identities)))
    if len({row[0] for row in result}) != len(result):
        raise ProviderError('reference_upload_unverified', '豆包原生图片引用重复；提示词未发送。', 409)
    return result


def _text(value, *, name=False):
    return (isinstance(value, str) and 0 < len(value) <= (255 if name else 512)
            and not any(ord(char) < 32 or (name and char in '/\\') for char in value))


async def _native_cards(page, selectors, *, allow_pending=False):
    cards, signatures, pending = [], [], False
    ready = page.locator(selectors['upload_ready'])
    for index in range(await ready.count()):
        card = ready.nth(index)
        if not await card.is_visible():
            raise ProviderError('reference_upload_unverified', '豆包图片原生附件卡不可见；提示词未发送。', 409)
        observed = await card.evaluate(_NATIVE_CARD, isolated_context=False)
        states = observed.get('states')
        if not isinstance(states, list) or not states or any(state != states[0] for state in states):
            raise _AttachmentStatePending('reference_upload_unverified', '豆包图片的原生附件状态缺失或不一致；提示词未发送。', 409)
        state = states[0]
        if (state.get('type') != 'image' or observed.get('type') != 'image'
                or not _text(state.get('name'), name=True) or observed.get('name') != state['name']
                or not _text(state.get('local_key')) or type(state.get('size')) is not int or state['size'] <= 0
                or type(state.get('percent')) not in (int, float) or not math.isfinite(state['percent'])
                or not 0 <= state['percent'] <= 100):
            raise ProviderError('reference_upload_unverified', '豆包图片原生附件类型、名称或身份未确认；提示词未发送。', 409)
        if state.get('status') == 'Uploading':
            if not allow_pending:
                raise ProviderError('reference_upload_changed', '豆包图片仍在上传；提示词未发送。', 409)
            pending = True
        elif state.get('status') != 'Normal':
            raise ProviderError('reference_upload_failed', '豆包图片原生上传失败或返回未知状态；提示词未发送。', 409)
        elif (state['percent'] != 100 or not _text(state.get('key'))
              or state.get('image_keys') != [state['key']]
              or any(type(state.get(key)) is not int or not 0 < state[key] <= 65535 for key in ('width', 'height'))):
            raise ProviderError('reference_upload_unverified', '豆包图片原生完成状态或引用标识不一致；提示词未发送。', 409)
        cards.append((state.get('key'), state['name'], 'image'))
        signatures.append(state)
    if (len({state['local_key'] for state in signatures}) != len(signatures)
            or len({row[0] for row in cards if row[0]}) != len([row for row in cards if row[0]])):
        raise ProviderError('reference_upload_unverified', '豆包图片原生附件身份重复；提示词未发送。', 409)
    return cards, signatures, pending


def image_references(references, capability):
    if capability.get('entry_mode') != 'ordinary_dialog' or capability.get('native_media_bridge') is True:
        raise ProviderError('unsupported_reference', '参考图片必须使用已识别的普通对话上传入口。')
    values = normalize_references(references, capability)
    if not values or any(item['kind'] != 'image' for item in values):
        raise ProviderError('unsupported_reference', '普通对话图片上传路径只接收参考图片。')
    _selectors(capability)
    return values


async def prepare_image_references(page, references, capability, *, timeout=180):
    values = image_references(references, capability)
    selectors = _selectors(capability)
    if _native(capability):
        if type(timeout) not in (int, float) or not math.isfinite(timeout) or timeout <= 0:
            raise ProviderError('reference_upload_timeout', '豆包参考图片上传等待时间无效。')
        deadline = time.monotonic() + timeout
        baseline, _, _ = await _native_cards(page, selectors)
        if baseline:
            raise ProviderError('reference_upload_unverified', '新对话仍有旧附件，未混用参考图片。', 409)
        # Hidden file inputs are valid; only this observed input must be unique.
        started=time.monotonic()
        input_deadline=min(deadline,started+8)
        control=page.locator(selectors['upload_image'])
        chooser_initialized=False
        while True:
            count=await control.count()
            if count>1:
                raise ProviderError('unsupported_reference','豆包图片上传入口定位不唯一；提示词未发送。')
            if count == 0:
                # Ordinary chat can add a layout wrapper around the observed
                # main input. Stay inside that same composer and exclude the
                # separately observed menu input; never choose a global input
                # or alter accept/multiple to make a reference fit.
                scoped_selector = '[data-testid="chat_input"] input[type="file"]:not([data-testid="upload-file-input"])'
                scoped = page.locator(scoped_selector)
                scoped_count = await scoped.count()
                if scoped_count > 1:
                    raise ProviderError('unsupported_reference','豆包编辑区存在多个图片上传入口；提示词未发送。')
                if scoped_count == 1:
                    control, count = scoped, 1
                    selectors = {**selectors, 'upload_image': scoped_selector}
                    logging.getLogger(__name__).info('Doubao image input resolved inside the ordinary composer')
                elif not chooser_initialized:
                    # The native add-material menu lazily mounts the primary
                    # composer input on some accounts. Open that observed
                    # control once; the menu's separate input remains excluded.
                    trigger=page.locator('[data-testid="chat_input"] [data-testid="upload_file_button"]').filter(visible=True)
                    remaining=input_deadline-time.monotonic()
                    if remaining>0 and await trigger.count()==1 and await trigger.is_enabled():
                        chooser_initialized=True
                        await trigger.click(timeout=min(1500,remaining*1000))
                        menus=page.get_by_role('menu').filter(visible=True)
                        if await menus.count()==1:
                            await page.keyboard.press('Escape')
                        logging.getLogger(__name__).info('Doubao native image chooser initialized')
                        continue
            if count==1 and not await control.is_disabled():break
            if time.monotonic()>=input_deadline:
                logging.getLogger(__name__).warning('Doubao image input unavailable: configured=%s, composer=%s, page=%s',
                    count, await page.locator('[data-testid="chat_input"] input[type="file"]').count(),
                    await page.locator('input[type="file"]').count())
                raise ProviderError('reference_upload_timeout','豆包图片上传入口未就绪或等待超时；提示词未发送。',503)
            await asyncio.sleep(min(.1,max(0,input_deadline-time.monotonic())))
        logging.getLogger(__name__).info('Doubao image input ready (%.2fs)',time.monotonic()-started)
        if (await _native_cards(page,selectors))[0]:
            raise ProviderError('reference_upload_unverified','等待图片入口时出现旧附件；提示词未发送。',409)
        # The generic uploader performs one native file-input action and waits
        # only until the new card exists. Its observed state below proves completion.
        await attach_native(page, values, {'upload_image': selectors['upload_image'],
                                         'upload_ready': selectors['upload_ready']}, timeout=max(.001,deadline-time.monotonic()))
        while True:
            try:
                cards, signatures, pending = await _native_cards(page, selectors, allow_pending=True)
            except _AttachmentStatePending:
                if time.monotonic()>=deadline:
                    raise ProviderError('reference_upload_timeout','豆包图片原生附件状态未在上传期限内稳定；提示词未发送。',503) from None
                await asyncio.sleep(min(.1,max(0,deadline-time.monotonic())))
                continue
            if not pending:
                break
            if time.monotonic() >= deadline:
                raise ProviderError('reference_upload_timeout', '豆包参考图片仍在上传；提示词未发送。', 503)
            await asyncio.sleep(min(.1, max(0, deadline - time.monotonic())))
        if (len(cards) != len(values)
                or any(card[1] != value['name'] or state['size'] != value['size_bytes']
                       for card, state, value in zip(cards, signatures, values))):
            raise ProviderError('reference_upload_unverified', '豆包原生参考图片数量、名称、大小或顺序不一致；提示词未发送。', 409)
        return {'cards': cards, 'page_url': page.url, 'selectors': dict(selectors),
                'native_state': 'attachment_state_v1', 'native_signature': signatures}
    if await _cards(page, selectors):
        raise ProviderError('reference_upload_unverified', '新对话仍有旧附件，未上传或混用参考图片。', 409)
    await attach_native(page, values, selectors, timeout=timeout)
    cards = await _cards(page, selectors)
    if len(cards) != len(values) or any(card[1] != value['name'] for card, value in zip(cards, values)):
        raise ProviderError('reference_upload_unverified', '豆包原生参考图片的数量、名称或顺序不一致；提示词未发送。', 409)
    return {'cards': cards, 'page_url': page.url, 'selectors': dict(selectors)}


async def assert_image_references(page, state):
    if not isinstance(state, dict) or page.url != state.get('page_url'):
        raise ProviderError('reference_upload_changed', '参考图片所属的豆包对话发生变化；提示词未发送。', 409)
    if state.get('native_state') == 'attachment_state_v1':
        cards, signatures, _ = await _native_cards(page, state['selectors'])
        if signatures != state.get('native_signature'):
            raise ProviderError('reference_upload_changed', '豆包原生图片附件身份或完成状态已改变；提示词未发送。', 409)
    else:
        cards = await _cards(page, state['selectors'])
    if cards != state.get('cards'):
        raise ProviderError('reference_upload_changed', '豆包原生参考图片已移除、替换或改变顺序；提示词未发送。', 409)
