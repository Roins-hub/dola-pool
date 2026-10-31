"""Resolve an observed Doubao video through its official download endpoint.

The download marker records the endpoint/URL evidence; it is not an image
analysis claim that every frame is free of watermarks. This module never
rewrites generation requests or calls arbitrary fallback APIs.
"""
import asyncio
import ipaddress
from pathlib import Path
import re
import socket
import struct
from urllib.parse import parse_qs, urlsplit

from media import _validate_url_async
from providers.base import ProviderError
from video_metadata import InvalidVideoError


_IDENTIFIER = re.compile(r'^[0-9]{8,64}$')
_MESSAGE = re.compile(r'^[A-Za-z0-9_-]{1,128}$')


RESOLVE_JS = r'''async ({videoUrl, nodeId, messageId}) => {
  if (location.protocol !== 'https:' || !['www.doubao.com','doubao.com'].includes(location.hostname))
    return {candidates:[]};
  const ids=[], records=[], mids=[];
  const id = value => typeof value === 'string' && /^\d{8,64}$/.test(value) ? value :
    (typeof value === 'number' && Number.isSafeInteger(value) && value >= 10000000 ? String(value) : null);
  const add = value => {const found=id(value); if(found && !ids.includes(found) && ids.length<8) ids.push(found)};
  const key = value => {
    try {return decodeURIComponent(new URL(value,location.href).pathname).replace(/~[^/]*$/,'').toLowerCase()}
    catch (_) {return ''}
  };
  const target=key(videoUrl);
  const visible = el => {const r=el.getBoundingClientRect(),s=getComputedStyle(el);
    return r.width>=48 && r.height>=48 && s.display!=='none' && s.visibility!=='hidden'};
  let card=null, matched=null;
  if(messageId) card=Array.from(document.querySelectorAll('[data-message-id]'))
    .find(el=>el.getAttribute('data-message-id')===messageId)||null;
  if(target) for(const el of document.querySelectorAll('video,img')) {
    if(!visible(el))continue;
    const ownCard=el.closest('[data-message-id]');
    if(card && ownCard!==card)continue;
    if([el.currentSrc,el.src,el.poster].filter(Boolean).some(value=>key(value)===target)) {
      if(!messageId || ownCard===card) {matched=el;card=ownCard;break}
    }
  }
  const scope=messageId || (card && card.getAttribute('data-message-id'));
  if(scope && id(scope))mids.push(scope);
  const seen=new WeakSet();let visited=0;
  const collect = (value,depth=0) => {
    if(!value || typeof value!=='object' || depth>8 || visited++>10000 || seen.has(value))return;
    seen.add(value);
    let marker;
    try {marker=value.message_id??value.messageId??value.message_id_str} catch(_){return}
    if(scope && marker!=null && String(marker)!==scope)return;
    let keys;try{keys=Object.keys(value).slice(0,120)}catch(_){return}
    for(const prop of keys) {
      if(['return','child','sibling','stateNode','_owner','alternate'].includes(prop))continue;
      let item;try{item=value[prop]}catch(_){continue}
      if(['node_id','nodeId','node_id_str','nodeIdStr'].includes(prop))add(item);
      else if(item && typeof item==='object')collect(item,depth+1);
    }
  };
  const inspect = start => {
    for(let el=start,level=0;el && level<12;el=el.parentElement,level++) {
      if(card && el!==card && !card.contains(el))break;
      for(const prop of Object.keys(el)) {
        if(prop.startsWith('__reactProps$'))collect(el[prop]);
        if(prop.startsWith('__reactFiber$')) {
          let fiber=el[prop];
          for(let hop=0;fiber && hop<80;hop++,fiber=fiber.return) {
            if(!card && hop>0)break;
            const state=fiber.stateNode;
            if(state instanceof Element && card && state!==card && !card.contains(state))break;
            collect(fiber.memoizedProps);collect(fiber.pendingProps);
            // A component without a DOM boundary must not lead us into the
            // thread's props and unrelated messages.
            if(!state && fiber!==el[prop])break;
          }
        }
      }
      if(el===card || !card)break;
    }
  };
  if(nodeId)add(nodeId);
  else {if(matched)inspect(matched);if(card)inspect(card)}
  const post = async (path,body) => {
    const controller=new AbortController(),timer=setTimeout(()=>controller.abort(),6000);
    try {
      const response=await fetch(path,{method:'POST',credentials:'include',redirect:'error',
        headers:{accept:'application/json','content-type':'application/json','agw-js-conv':'str'},
        body:JSON.stringify(body),signal:controller.signal});
      if(!response.ok)return null;
      const reader=response.body.getReader(),parts=[];let size=0;
      while(true) {const part=await reader.read();if(part.done)break;
        size+=part.value.byteLength;if(size>1048576){await reader.cancel();return null}parts.push(part.value)}
      const bytes=new Uint8Array(size);let offset=0;for(const part of parts){bytes.set(part,offset);offset+=part.byteLength}
      const result=JSON.parse(new TextDecoder().decode(bytes));
      return result && result.code===0 ? result.data : null;
    } catch(_){return null} finally {clearTimeout(timer)}
  };
  if(!ids.length && mids.length) {
    const result=await post('/samantha/aispace/message_node_info',{message_ids_str:mids});
    // This response belongs only to the explicit/matched message IDs above.
    if(result)collect(result);
  }
  for(const node of ids.slice(0,3)) {
    const data=await post('/samantha/aispace/get_download_info',{requests:[{node_id:node}]});
    const infos=data && data.download_infos;
    if(!Array.isArray(infos))continue;
    for(const info of infos.slice(0,12)) {
      if(!info || typeof info!=='object')continue;
      const own=id(info.node_id??info.node_id_str);
      if(own && own!==node)continue;
      for(const field of ['main_url','backup_url']) {
        const value=info[field];
        if(typeof value==='string' && value.length<=4096)records.push({url:value,node_id:node});
      }
    }
  }
  return {candidates:records.slice(0,24)};
}'''


def _https_public_shape(value):
    """Cheap rejection before DNS, including alternate IPv4 literal spellings."""
    if not isinstance(value, str) or not 0 < len(value) <= 4096 or any(ord(c) <= 32 for c in value):
        return False
    try:
        parsed = urlsplit(value)
        host = parsed.hostname
        if parsed.scheme != 'https' or not host or parsed.username or parsed.password or parsed.port not in (None, 443):
            return False
        if host.lower() == 'localhost' or host.lower().endswith(('.localhost', '.local', '.internal', '.lan')):
            return False
        try:
            address = ipaddress.ip_address(host)
        except ValueError:
            try:
                address = ipaddress.ip_address(socket.inet_aton(host))
            except OSError:
                return '.' in host and not host.isdecimal()
        return address.is_global
    except (ValueError, TypeError):
        return False


def _download_marker(url):
    from video_delivery import known_watermarked
    if known_watermarked(url):
        return False
    parsed = urlsplit(url)
    host = parsed.hostname.lower()
    query = parse_qs(parsed.query)
    if query.get('download', [''])[0].lower() != 'true':
        return False
    if host in ('videoweb-download.doubao.com', 'videoweb.doubao.com'):
        return True
    if (host == 'doubao.com' or host.endswith('.doubao.com')) and '/download' in parsed.path.lower():
        return True
    return any(query.get(key, [''])[0].lower() in ('video_mp4', 'video/mp4')
               for key in ('mime_type', 'format'))


async def resolve_download(page, video_url, *, node_id=None, message_id=None, require_no_watermark=False):
    """Resolve only the original video's evidence, keeping node IDs as strings.

    Every returned URL passes HTTPS/public-DNS validation. The caller must
    revalidate immediately before transferring bytes on the account's route.
    """
    if node_id is not None and (not isinstance(node_id, str) or not _IDENTIFIER.fullmatch(node_id)):
        raise ProviderError('invalid_media', '豆包成片节点编号无效，未请求下载。')
    if message_id is not None and (not isinstance(message_id, str) or not _MESSAGE.fullmatch(message_id)):
        raise ProviderError('invalid_media', '豆包成片消息编号无效，未请求下载。')
    records = []
    try:
        parsed = urlsplit(page.url)
        if parsed.scheme == 'https' and parsed.hostname in ('www.doubao.com', 'doubao.com') and parsed.port in (None, 443):
            data = await asyncio.wait_for(page.evaluate(RESOLVE_JS, {
                'videoUrl': video_url, 'nodeId': node_id, 'messageId': message_id}), timeout=20)
            if isinstance(data, dict) and isinstance(data.get('candidates'), list):
                records = data['candidates'][:24]
    except (asyncio.CancelledError, KeyboardInterrupt, SystemExit):
        raise
    except Exception:
        # Failing to read an official response never upgrades preview evidence.
        pass
    candidates = []
    seen = set()
    for record in records + [{'url': video_url, 'preview': True}]:
        if not isinstance(record, dict):
            continue
        url = record.get('url')
        if not _https_public_shape(url) or url in seen:
            continue
        seen.add(url)
        try:
            await _validate_url_async(url)
        except (ValueError, OSError, TimeoutError):
            continue
        original = bool(record.get('preview'))
        verified = not original and _download_marker(url)
        candidate = {'url': url, 'source': 'preview' if original else ('official_download' if verified else 'official_preview'),
                     'node_id': record.get('node_id') if isinstance(record.get('node_id'), str) else None,
                     'watermark': 'verified_download' if verified else 'unknown'}
        if not require_no_watermark or verified:
            candidates.append(candidate)
    candidates.sort(key=lambda item: item['watermark'] != 'verified_download')
    if not candidates:
        if require_no_watermark:
            raise ProviderError('no_watermark_unavailable', '未确认当前成片的官方无水印下载地址，未降级为预览文件。', 503)
        raise ProviderError('invalid_media', '豆包成片地址不是有效的 HTTPS 公网地址。', 503)
    return {**candidates[0], 'alternatives': candidates[1:]}


def read_video_dimensions(path):
    """Read displayed video dimensions from bounded MP4 boxes, without ffmpeg.

    Unknown track metadata returns None. Broken/truncated containers raise
    InvalidVideoError; duration/integrity still requires read_video_duration.
    """
    path = Path(path)
    size = path.stat().st_size
    kinds = set()
    dimensions = []
    count = 0
    with path.open('rb') as stream:
        def boxes(start, end):
            nonlocal count
            offset = start
            while offset < end:
                count += 1
                if count > 100000 or end - offset < 8:
                    raise InvalidVideoError('MP4 容器结构异常或不完整')
                stream.seek(offset)
                length, kind = struct.unpack('>I4s', stream.read(8))
                header = 8
                if length == 1:
                    if end - offset < 16:
                        raise InvalidVideoError('MP4 文件头不完整')
                    length = struct.unpack('>Q', stream.read(8))[0]
                    header = 16
                elif length == 0:
                    length = end - offset
                if length < header or offset + length > end:
                    raise InvalidVideoError('MP4 文件被截断')
                yield kind, offset + header, offset + length
                offset += length

        def track(start, end):
            header = None
            handler = None
            for kind, left, right in boxes(start, end):
                if kind == b'tkhd':
                    stream.seek(left)
                    data = stream.read(min(right-left, 96))
                    if len(data) < 4 or data[0] not in (0, 1):
                        raise InvalidVideoError('MP4 视频轨头无效')
                    required = 96 if data[0] else 84
                    if right-left < required:
                        raise InvalidVideoError('MP4 视频轨头不完整')
                    matrix_offset = 52 if data[0] else 40
                    width, height = struct.unpack('>II', data[required-8:required])
                    a, b, _, c, d = struct.unpack('>5i', data[matrix_offset:matrix_offset+20])
                    header = (width / 65536, height / 65536, a, b, c, d)
                elif kind == b'mdia':
                    for child, child_left, child_right in boxes(left, right):
                        if child == b'hdlr':
                            if child_right-child_left < 12:
                                raise InvalidVideoError('MP4 轨道类型头不完整')
                            stream.seek(child_left+8)
                            handler = stream.read(4)
            if handler == b'vide' and header:
                width, height, a, b, c, d = header
                if not 0 < width <= 65535 or not 0 < height <= 65535:
                    raise InvalidVideoError('MP4 视频尺寸无效')
                if a == 0 and d == 0 and abs(b) == 65536 and abs(c) == 65536:
                    width, height = height, width
                dimensions.append({'width': round(width), 'height': round(height)})

        for kind, left, right in boxes(0, size):
            kinds.add(kind)
            if kind == b'moov':
                for child, child_left, child_right in boxes(left, right):
                    if child == b'trak':
                        track(child_left, child_right)
    if not {b'ftyp', b'moov', b'mdat'}.issubset(kinds):
        raise InvalidVideoError('下载内容不是完整 MP4 视频')
    return max(dimensions, key=lambda item: item['width'] * item['height']) if dimensions else None
