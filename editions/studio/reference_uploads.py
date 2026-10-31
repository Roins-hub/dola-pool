"""Owned, private reference files. Incoming uploads expire after one day."""
from io import BytesIO
import json
from pathlib import Path
import re
import shutil
import time
import uuid
from PIL import Image

IMAGE_ID = re.compile(r'[a-f0-9]{32}\.(?:jpg|png|webp)')
MEDIA_ID = re.compile(r'[a-f0-9]{32}\.(?:mp3|wav|m4a|aac|flac|ogg|mp4|mov|webm)')
FILE_ID = re.compile(r'[a-f0-9]{32}\.(?:jpg|png|webp|mp3|wav|m4a|aac|flac|ogg|mp4|mov|webm)')
TASK_ID = re.compile(r'video_[a-f0-9]{32}')


def admin_owner(identity):
    return ('user:' + str(identity['user']['id']) if identity.get('kind') == 'session'
            else identity.get('kind', 'admin'))

def api_owner(client):
    """Only authenticated principals own uploads; key scopes never become users."""
    user_id = client.get('owner_user_id')
    if user_id is not None:
        return 'user:' + str(user_id)
    key_hash = client.get('api_key_hash')
    if isinstance(key_hash, str) and re.fullmatch(r'[a-f0-9]{64}', key_hash):
        return 'api-key:' + key_hash
    return None


def save_image(data, root, owner, max_bytes):
    if not data or len(data) > max_bytes:
        raise ValueError('图片为空或超过单张大小限制。')
    try:
        with Image.open(BytesIO(data)) as image:
            suffix = {'PNG': '.png', 'JPEG': '.jpg', 'WEBP': '.webp'}.get(image.format)
            width, height = image.size
            if not suffix or width * height > 50_000_000:
                raise ValueError()
            image.verify()
    except Exception:
        raise ValueError('请使用有效的 JPG、PNG 或 WebP 图片，且不超过 5000 万像素。') from None
    result = _save_file(data, suffix, root, owner)
    return {**result, 'width': width, 'height': height}


def save_media(data, filename, root, owner, max_bytes):
    """Check extension and container signature; the native uploader validates codecs."""
    suffix = Path(filename).suffix.lower()
    if not MEDIA_ID.fullmatch('0' * 32 + suffix):
        raise ValueError('支持 MP3、WAV、M4A、AAC、FLAC、OGG 音频及 MP4、MOV、WebM 视频。')
    if not data or len(data) > max_bytes:
        raise ValueError('素材为空或超过单个文件大小限制。')
    valid = False
    if suffix == '.wav':
        import wave
        try:
            with wave.open(BytesIO(data)) as sound:
                valid = sound.getnframes() > 0 and bool(sound.readframes(1))
        except (wave.Error, EOFError):
            pass
    elif suffix in ('.mp4', '.mov', '.m4a'):
        # ISO BMFF/QuickTime must contain complete top-level boxes and media data.
        offset, kinds = 0, set()
        while offset + 8 <= len(data):
            size = int.from_bytes(data[offset:offset+4], 'big')
            kind = data[offset+4:offset+8]
            header = 8
            if size == 1 and offset + 16 <= len(data):
                size = int.from_bytes(data[offset+8:offset+16], 'big')
                header = 16
            elif size == 0:
                size = len(data) - offset
            if size < header or offset + size > len(data):
                break
            if size > header: kinds.add(kind)
            offset += size
        valid = offset == len(data) and {b'moov', b'mdat'}.issubset(kinds)
    elif suffix == '.webm':
        valid = data.startswith(b'\x1a\x45\xdf\xa3') and b'webm' in data[:4096] and len(data) > 64
    elif suffix == '.flac':
        valid = data.startswith(b'fLaC') and len(data) > 42
    elif suffix == '.ogg':
        valid = data.startswith(b'OggS') and any(x in data[:4096] for x in (b'OpusHead', b'\x01vorbis'))
    elif suffix == '.aac':
        valid = len(data) > 7 and data[0] == 255 and data[1] & 0xf6 == 0xf0
    elif suffix == '.mp3':
        start = 0
        if data.startswith(b'ID3') and len(data) >= 10:
            start = 10 + sum((data[6+i] & 127) << (7*(3-i)) for i in range(4))
        valid = any(data[i] == 255 and data[i+1] & 0xe0 == 0xe0
                    and data[i+1] & 6 != 0 and data[i+2] & 0xf0 not in (0, 0xf0)
                    for i in range(start, min(len(data)-3, start+4096)))
    if not valid:
        raise ValueError('文件内容与音视频格式不符或文件不完整，请重新选择原始素材。')
    name = filename.replace('\\', '/').rsplit('/', 1)[-1][:200]
    kind = 'video' if suffix in ('.mp4', '.mov', '.webm') else 'audio'
    return {**_save_file(data, suffix, root, owner), 'kind': kind, 'name': name}


def _save_file(data, suffix, root, owner):
    folder = Path(root).resolve() / 'incoming'
    folder.mkdir(parents=True, exist_ok=True)
    identifier = uuid.uuid4().hex + suffix
    path = folder / identifier
    meta = path.with_suffix(path.suffix + '.json')
    try:
        with path.open('xb') as output:
            output.write(data)
        meta.write_text(json.dumps({'owner': owner, 'created_at': time.time()}), encoding='utf-8')
    except Exception:
        path.unlink(missing_ok=True)
        meta.unlink(missing_ok=True)
        raise
    # Incoming images are reusable for one day. Tasks get independent copies.
    for old in folder.glob('*.json'):
        image_name = old.name[:-5]
        try:
            if FILE_ID.fullmatch(image_name) and old.stat().st_mtime < time.time() - 86400:
                (folder / image_name).unlink(missing_ok=True)
                old.unlink(missing_ok=True)
        except OSError:
            pass
    return {'id': identifier, 'size': len(data)}

def attach_images(ids, root, owner, task_id, *, media_ids=None, media_max_count=10):
    if not TASK_ID.fullmatch(task_id):
        raise ValueError('任务标识无效。')
    media_ids = list(dict.fromkeys(media_ids or []))
    if len(media_ids) > media_max_count:
        raise ValueError(f'音频和视频合计最多 {media_max_count} 个。')
    if any(not IMAGE_ID.fullmatch(i) for i in ids):
        raise ValueError('图片标识无效，请重新选择图片。')
    if any(not MEDIA_ID.fullmatch(i) for i in media_ids):
        raise ValueError('音视频素材标识无效，请重新选择素材。')
    if not ids and not media_ids:
        return []
    root = Path(root).resolve()
    sources = []
    for identifier in dict.fromkeys([*ids, *media_ids]):
        source = root / 'incoming' / identifier
        try:
            metadata = json.loads(source.with_suffix(source.suffix + '.json').read_text(encoding='utf-8'))
            if (metadata['owner'] != owner or metadata['created_at'] < time.time() - 86400
                    or not source.is_file() or source.resolve().parent != root / 'incoming'):
                raise ValueError()
        except (OSError, ValueError, KeyError, TypeError):
            raise ValueError('参考素材不可用、已过期或不属于当前用户，请重新选择。') from None
        sources.append(source)
    destination = root / 'tasks' / task_id
    destination.mkdir(parents=True, exist_ok=False)
    try:
        for source in sources:
            shutil.copyfile(source, destination / source.name)
    except Exception:
        cleanup_task_images(root, task_id)
        raise
    return [f'upload:{task_id}/{source.name}' for source in sources]

def copy_task_image(reference, root, task_id, destination):
    prefix = f'upload:{task_id}/'
    if not TASK_ID.fullmatch(task_id) or not reference.startswith(prefix):
        raise ValueError('参考图片不属于当前任务。')
    identifier = reference[len(prefix):]
    if not FILE_ID.fullmatch(identifier):
        raise ValueError('参考素材标识无效。')
    folder = Path(root).resolve() / 'tasks' / task_id
    source = folder / identifier
    if not source.is_file() or source.resolve().parent != folder:
        raise ValueError('任务的参考图片不存在，请重新选择后提交。')
    target = Path(destination).with_suffix(source.suffix)
    shutil.copyfile(source, target)
    return target

def cleanup_task_images(root, task_id):
    if not TASK_ID.fullmatch(task_id):
        raise ValueError('任务标识无效。')
    parent = Path(root).resolve() / 'tasks'
    target = parent / task_id
    if target.exists() and target.resolve().parent == parent:
        # Best effort: a temporarily locked file must not prevent slot release.
        shutil.rmtree(target, ignore_errors=True)
