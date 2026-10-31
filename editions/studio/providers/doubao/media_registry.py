"""Durable media identity without confusing signed CDN aliases for new videos."""
import re
from urllib.parse import urlsplit, parse_qsl, urlencode, urlunsplit

_VOLATILE=re.compile(r'^(?:sign|signature|token|expires?|expire_time|x-expires|x-signature|auth_key|a_bogus|mstoken|timestamp|ts)$',re.I)
_NODE=re.compile(r'^\d{8,30}$')


def url_key(value):
    if not isinstance(value,str) or len(value)>8192:
        return None
    try:
        parsed=urlsplit(value)
        if parsed.scheme!='https' or not parsed.hostname or parsed.username or parsed.password:
            return None
        query=sorted((key,value) for key,value in parse_qsl(parsed.query,keep_blank_values=True) if not _VOLATILE.fullmatch(key))
        return urlunsplit(('https',parsed.netloc.lower(),parsed.path,urlencode(query),''))
    except ValueError:
        return None


def identity_keys(record):
    if not isinstance(record,dict):return set()
    keys=set()
    node=record.get('node_id')
    if isinstance(node,str) and _NODE.fullmatch(node):keys.add('node:'+node)
    for field,prefix in (('fallback_api','fb:'),('url','url:')):
        key=url_key(record.get(field))
        if key:keys.add(prefix+key)
    return keys


class VideoRegistry:
    def __init__(self,conversation_id,*,records=(),keys=(),urls=()):
        self.conversation_id=conversation_id
        self._keys={key for key in keys if isinstance(key,str) and key.startswith(('node:','fb:','url:')) and len(key)<8200}
        for record in records:
            if record.get('conversation_id')==conversation_id:self._keys.update(identity_keys(record))
        self._keys.update('url:'+key for url in urls if (key:=url_key(url)))

    def keys(self):return sorted(self._keys)

    def is_new(self,record):
        if not self.conversation_id or record.get('conversation_id')!=self.conversation_id or not record.get('message_id'):
            return False
        keys=identity_keys(record)
        return bool(keys and not keys.intersection(self._keys))
