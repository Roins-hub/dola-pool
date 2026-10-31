"""Build a video-17 probe from observed native controls and owned materials."""
import time
import uuid
from pathlib import Path

from providers.base import ProviderError
from reference_uploads import attach_images, cleanup_task_images
from .materials import normalize_video_spec


def verified_request(request,model,version):
    import json
    from reference_uploads import IMAGE_ID,MEDIA_ID
    spec=normalize_video_spec(request)
    features=model.get('features',{})
    resolution=spec['resolution']
    if resolution!='auto' and (not features.get('resolution',{}).get('enabled')
            or resolution not in features['resolution']['values']):
        raise ProviderError('unsupported_resolution','此豆包模型尚未验证该清晰度。')
    if not any(c['duration']==spec['duration'] and c['ratio']==spec['ratio']
               and (resolution=='auto' or c.get('resolution','auto')==resolution)
               for c in model.get('combinations',[])):
        raise ProviderError('unsupported_duration','此豆包时长、比例及清晰度组合尚未验证。')
    watermark=request.get('require_no_watermark',False)
    if type(watermark) is not bool or watermark and not features.get('no_watermark',{}).get('enabled'):
        raise ProviderError('no_watermark_unavailable','此模型的官方无水印下载尚未验证。')
    images=request.get('reference_images') or []
    if isinstance(images,str):
        try:images=json.loads(images)
        except ValueError:raise ProviderError('unsupported_reference','参考图片列表无效。') from None
    image_paths=request.get('reference_image_paths') or []
    image_tokens=request.get('reference_uploads') or []
    media_tokens=request.get('reference_media_uploads') or []
    media_paths=request.get('reference_media_paths') or []
    if any(not isinstance(v,list) for v in (images,image_paths,image_tokens,media_tokens,media_paths)):
        raise ProviderError('unsupported_reference','参考素材列表无效。')
    if any(not isinstance(v,str) or not IMAGE_ID.fullmatch(v) for v in image_tokens) or any(not isinstance(v,str) or not MEDIA_ID.fullmatch(v) for v in media_tokens):
        raise ProviderError('unsupported_reference','参考素材标识无效。')
    if any(not isinstance(v,dict) or v.get('kind') not in ('audio','video') for v in media_paths):
        raise ProviderError('unsupported_reference','参考音视频列表无效。')
    counts={'images':len(images)+max(len(image_paths),len(image_tokens))}
    for kind in ('audio','video'):
        tokens=sum(('video' if Path(v).suffix in ('.mp4','.mov','.webm') else 'audio')==kind for v in media_tokens)
        counts[kind]=max(tokens,sum(v['kind']==kind for v in media_paths))
    for kind,count in counts.items():
        cap=model.get('references',{}).get(kind,{})
        if count and (not cap.get('enabled') or count>cap['max_count']):
            raise ProviderError('unsupported_reference','所选参考素材类型或数量尚未验证。')
    for key in ('reference_audio','reference_audios','reference_video','reference_videos','reference_media'):
        if request.get(key) not in (None,'',[],'[]'):
            raise ProviderError('unsupported_reference','参考音视频请使用本平台的上传标识。')
    return {**request,**spec,'platform':'doubao','submitted_duration':spec['duration'],
            'reference_images':images,'reference_image_paths':image_paths,'reference_media_paths':media_paths,
            'capability_version':version}


def native_plan(provider,account,request,checks,*,upload_owner=None,upload_root=None,native_model):
    forbidden={'reference_image_paths','reference_media_paths','reference_upload_root'}
    if forbidden.intersection(request) or any(key.startswith('_') for key in request):
        raise ProviderError('unsupported_reference','本地素材必须通过当前管理员的上传标识引用。')
    spec=normalize_video_spec(request)
    if not isinstance(native_model,dict) or native_model.get('id')!=spec['model'] or not native_model.get('label'):
        raise ProviderError('native_state_changed','此账号未观测到选择的原生视频模型，未提交。')
    if type(native_model.get('max_duration')) is not int or spec['duration']>native_model['max_duration']:
        raise ProviderError('unsupported_duration','请求时长不属于此账号的原生控件选项，未提交。')
    if request.get('duration_profile'):
        raise ProviderError('unsupported_duration','豆包不使用 Dola 的时长映射。')
    if type(request.get('require_no_watermark',False)) is not bool:
        raise ProviderError('invalid_request','无水印要求必须是明确的布尔值。')
    images=request.get('reference_images') or []
    image_ids=request.get('reference_uploads') or []
    media_ids=request.get('reference_media_uploads') or []
    if any(not isinstance(value,list) for value in (images,image_ids,media_ids)) or len(images)+len(image_ids)+len(media_ids)>50:
        raise ProviderError('unsupported_reference','素材列表格式或数量不正确。')
    if (image_ids or media_ids) and (not upload_owner or upload_root is None):
        raise ProviderError('unsupported_reference','参考素材必须属于当前已登录管理员。')
    previous=next((m for m in provider.manifest.load()['models'] if m['id']==spec['model']),None)
    if (images or image_ids or media_ids) and not previous:
        raise ProviderError('unsupported_reference','请先完成此模型的纯文本成片验证，再验证参考素材。')
    identifier='video_'+uuid.uuid4().hex
    paths=[];media_paths=[];folder=None
    try:
        if image_ids or media_ids:
            attach_images(image_ids,upload_root,upload_owner,identifier,media_ids=media_ids)
            folder=(Path(upload_root)/'tasks'/identifier).resolve()
            paths=[str(folder/item) for item in image_ids]
            for item in media_ids:
                kind='video' if Path(item).suffix.lower() in ('.mp4','.mov','.webm') else 'audio'
                media_paths.append({'path':str(folder/item),'kind':kind})
        refs={'images':{'enabled':bool(images or paths),'max_count':max(1,len(images)+len(paths)),'max_bytes':15*1024*1024},
              'audio':{'enabled':any(v['kind']=='audio' for v in media_paths),'max_count':max(1,sum(v['kind']=='audio' for v in media_paths)),'max_bytes':50*1024*1024},
              'video':{'enabled':any(v['kind']=='video' for v in media_paths),'max_count':max(1,sum(v['kind']=='video' for v in media_paths)),'max_bytes':50*1024*1024}}
        selectors=dict(checks.get('observed_selectors') or {})
        if any(not selectors.get(key) for key in ('composer','send','assistant')):
            raise ProviderError('native_state_changed','豆包原生输入、发送或独立助手消息入口尚未识别，未提交。')
        combo={'duration':spec['duration'],'ratio':spec['ratio']}
        if spec['resolution']!='auto':combo['resolution']=spec['resolution']
        capability={'id':native_model['id'],'label':native_model['label'],'native_media_bridge':True,
            'enabled':True,'durations':[spec['duration']],'ratios':[spec['ratio']],
            'selectors':selectors,'combinations':[combo],'references':refs,'features':{},
            'receipt_patterns':['正在生成视频','视频生成中','开始生成','已开始生成','视频任务已提交','排队中'],
            'rejection_patterns':['无法生成视频','生成失败','违反','额度不足','需要升级','请升级','开通专业版','仅限专业版'],
            'no_charge_patterns':['未消耗','不会扣除','额度不足','需要升级','请升级','开通专业版','仅限专业版'],
            'evidence':{}}
        normalized={**request,**spec,'id':identifier,'platform':'doubao','submitted_duration':spec['duration'],
            'capability_version':'calibration-'+identifier,'reference_images':images,
            'reference_image_paths':paths,'reference_media_paths':media_paths}
        if folder:normalized['reference_upload_root']=str(folder)
        return {'account':account,'request':normalized,'capability':capability},previous
    except Exception:
        if folder:cleanup_task_images(upload_root,identifier)
        raise


def validate_completed_native(plan,result,job_id):
    from .downloads import read_video_dimensions
    request,model=plan['request'],plan['capability']
    dimensions=result.get('dimensions') or read_video_dimensions(result['output_path'])
    if not dimensions:
        raise ProviderError('video_metadata_missing','成片尺寸尚未确认，未开放此参数组合。')
    width,height=dimensions['width'],dimensions['height']
    a,b=map(int,request['ratio'].split(':'))
    if abs(width/height-a/b)>max(.02,(a/b)*.03):
        raise ProviderError('ratio_mismatch','实际成片比例与请求不符，保留成片，未开放该组合。')
    resolution=request.get('resolution','auto')
    if resolution!='auto':
        if min(width,height)<int(resolution[:-1]):
            raise ProviderError('resolution_mismatch','实际成片尺寸未达到请求清晰度，保留成片，未开放该清晰度。')
        model['features']['resolution']={'enabled':True,'values':[resolution]}
        model['evidence']['resolution_task_id']=job_id
    if request.get('require_no_watermark'):
        if result.get('watermark')!='verified_download':
            raise ProviderError('no_watermark_unavailable','未确认官方无水印下载来源，未开放无水印选项。')
        model['features']['no_watermark']={'enabled':True}
        model['evidence']['no_watermark_task_id']=job_id
    for kind,key in (('images','image_task_id'),('audio','audio_task_id'),('video','video_task_id')):
        if model['references'][kind]['enabled']:model['evidence'][key]=job_id
    return dimensions
