"""Read-only ordinary-chat attachment checks around the native file uploader.

No video-17 adapter, input accept rewrite, request construction, or signing.
Selectors and attachment metadata must be observed on the real native page
before a capability using this path can be registered.
"""
import re

from providers.base import ProviderError
from .materials import attach_native, normalize_references


_CARD = '''element=>{
  const records=[];
  const key=element.getAttribute('data-attachment-key');
  const name=element.getAttribute('data-file-name');
  const type=element.getAttribute('data-attachment-type')||element.getAttribute('data-kind');
  if(key!==null||name!==null||type!==null)records.push({key,name,type});
  // Inspect only objects explicitly attached to this exact native card's own
  // React props. Never climb into a thread, sibling, or whole-page store.
  const seen=new WeakSet();
  function visit(value,depth=0){
    if(!value||typeof value!=='object'||depth>3||seen.has(value))return;
    seen.add(value);
    const key=value.attachmentKey??value.attachment_key;
    if(key!==undefined){
      let type=value.type??value.kind;
      if(type===2&&value.media?.media_type===2)type='video';
      records.push({key,name:value.name??value.fileName??value.file?.name,type});
    }
    for(const field of ['attachment','attachmentState','reference'])visit(value[field],depth+1);
  }
  for(const field of Object.keys(element))if(field.startsWith('__reactProps$'))visit(element[field]);
  return records;
}'''


def _selectors(capability):
    selectors=capability.get('selectors',{})
    required=('upload_ready','upload_pending','upload_error')
    if any(not isinstance(selectors.get(key),str) or not selectors[key].strip() for key in required):
        raise ProviderError('unsupported_reference','豆包普通对话的附件完成、等待或失败状态尚未识别；提示词未发送。')
    direct=selectors.get('upload_video') or selectors.get('upload')
    chooser=selectors.get('upload_trigger') and selectors.get('upload_option')
    if not direct and not chooser:
        raise ProviderError('unsupported_reference','豆包普通对话的原生视频上传入口尚未识别；提示词未发送。')
    return selectors


async def _cards(page,selectors):
    if await page.locator(selectors['upload_pending']).count() or await page.locator(selectors['upload_error']).count():
        raise ProviderError('reference_upload_failed','豆包原生附件仍在上传或已失败；提示词未发送。',409)
    result=[]
    ready=page.locator(selectors['upload_ready'])
    for index in range(await ready.count()):
        card=ready.nth(index)
        if not await card.is_visible():
            raise ProviderError('reference_upload_unverified','豆包原生附件完成卡不可见，未确认参考视频。',409)
        records=await card.evaluate(_CARD,isolated_context=False)
        identities=set()
        for record in records:
            key,name,kind=record.get('key'),record.get('name'),record.get('type')
            if (not isinstance(key,str) or not 0<len(key)<=512 or any(ord(c)<32 for c in key)
                or not isinstance(name,str) or not 0<len(name)<=255 or re.search(r'[/\\\x00-\x1f]',name)
                or kind!='video'):
                raise ProviderError('reference_upload_unverified','豆包未明确确认原生视频附件的类型、名称和引用标识；提示词未发送。',409)
            identities.add((key,name,kind))
        if len(identities)!=1:
            raise ProviderError('reference_upload_unverified','豆包原生附件引用缺失或不唯一，未确认参考视频。',409)
        result.append(next(iter(identities)))
    if len({row[0] for row in result})!=len(result):
        raise ProviderError('reference_upload_unverified','豆包原生附件引用重复；提示词未发送。',409)
    return result


def video_references(references,capability):
    if capability.get('entry_mode')!='ordinary_dialog' or capability.get('native_media_bridge') is True:
        raise ProviderError('unsupported_reference','参考视频必须使用已验证的普通对话上传入口。')
    values=normalize_references(references,capability)
    if not values or any(item['kind']!='video' for item in values):
        raise ProviderError('unsupported_reference','普通对话此路径只接收已验证的参考视频。')
    _selectors(capability)
    return values


async def prepare_video_references(page,references,capability,*,timeout=180):
    values=video_references(references,capability)
    selectors=_selectors(capability)
    baseline=await _cards(page,selectors)
    # A new dialog must not accidentally send a leftover attachment. The empty
    # native baseline is checked before any file input or chooser is touched.
    if baseline:
        raise ProviderError('reference_upload_unverified','新对话仍有旧附件，未上传或混用参考视频。',409)
    await attach_native(page,values,selectors,timeout=timeout)
    cards=await _cards(page,selectors)
    if len(cards)!=len(values) or any(card[1]!=value['name'] for card,value in zip(cards,values)):
        raise ProviderError('reference_upload_unverified','豆包原生参考视频的数量、名称或顺序不一致；提示词未发送。',409)
    return {'cards':cards,'page_url':page.url,'selectors':dict(selectors)}


async def assert_video_references(page,state):
    if not isinstance(state,dict) or page.url!=state.get('page_url'):
        raise ProviderError('reference_upload_changed','参考视频所属的豆包对话发生变化；提示词未发送。',409)
    cards=await _cards(page,state['selectors'])
    if cards!=state.get('cards'):
        raise ProviderError('reference_upload_changed','豆包原生参考视频已移除、替换或改变顺序；提示词未发送。',409)


async def composer_signature(composer):
    """Compare prompt text across textarea and native paragraph re-renders."""
    return await composer.evaluate('''element=>{
      const normalize=text=>text.replace(/\\r\\n?/g,'\\n');
      if(typeof element.value==='string')return normalize(element.value);
      const block=node=>node.nodeType===Node.ELEMENT_NODE&&
        /^(P|DIV|LI|PRE|BLOCKQUOTE|H[1-6])$/.test(node.tagName);
      function content(node){
        if(node.nodeType===Node.TEXT_NODE)return normalize(node.nodeValue||'');
        if(node.nodeType!==Node.ELEMENT_NODE||node.matches('[contenteditable="false"]'))return '';
        if(node.tagName==='BR')return node.classList.contains('ProseMirror-trailingBreak')?'':'\\n';
        if(/^(P|DIV)$/.test(node.tagName)&&node.childNodes.length===1&&node.firstChild.nodeName==='BR')return '';
        const parts=[];let inline='';
        for(const child of node.childNodes){
          if(child.nodeType===Node.ELEMENT_NODE&&child.matches('[contenteditable="false"]'))continue;
          if(block(child)){
            if(inline!==''){parts.push(inline);inline='';}
            parts.push(content(child));
          }else inline+=content(child);
        }
        if(inline!==''||!parts.length)parts.push(inline);
        return parts.join('\\n');
      }
      return content(element);
    }''')
