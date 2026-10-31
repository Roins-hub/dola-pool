/* Keep local previews in memory; upload only when the user submits a video. */
window.ReferenceImages=(()=>{
  const types=new Set(['image/jpeg','image/png','image/webp']);
  const audio=new Set(['mp3','wav','m4a','aac','flac','ogg']),video=new Set(['mp4','mov','webm']);
  function create({api:request,query,prefix='genRef',basePath='/api/admin/reference-images',media=false}){
    let selected=[],busy=false,enabled=true,modelLimits={},serverLimits=media?{max_count:10,max_bytes:50*1024*1024}:{max_count:30,max_bytes:15*1024*1024},limits={...serverLimits};
    const kind=file=>audio.has(file.name.split('.').pop().toLowerCase())?'audio':video.has(file.name.split('.').pop().toLowerCase())?'video':'';
    const element=suffix=>query('#'+prefix+suffix);
    const currentPlatform=()=>query(prefix.startsWith('gen')?'#genPlatform':'#memberPlatform')?.value||'dola';
    const platformPath=(path,platform=currentPlatform())=>platform==='dola'?path:path+'?platform='+encodeURIComponent(platform);
    const inputs=[element('Files'),element('Folder')].filter(Boolean);
    function showLimits(){
      limits={...serverLimits};for(const key of ['max_count','max_bytes'])if(Number.isInteger(modelLimits[key])&&modelLimits[key]>0)limits[key]=Math.min(limits[key],modelLimits[key]);
      element('Help').textContent=media?`支持 MP3、WAV、M4A、AAC、FLAC、OGG 音频和 MP4、MOV、WebM 视频。单个最多 ${Math.round(limits.max_bytes/1024/1024)} MB，合计最多 ${limits.max_count} 个。点击“生成视频”时上传；Dola 确认素材就绪后才发送。`:`支持 JPG、PNG、WebP，单张最多 ${Math.round(limits.max_bytes/1024/1024)} MB，${modelLimits.local_only?'本地图片最多':'本地图片和链接合计最多'} ${limits.max_count} 张。点击“生成视频”时上传。`;
    }
    async function loadLimits(platform=currentPlatform()){
      try{const config=await request(platformPath(basePath+'/config',platform));for(const key of ['max_count','max_bytes'])if(Number.isInteger(config[key])&&config[key]>0)serverLimits[key]=config[key];}catch{}
      showLimits();
    }
    function render(){
      const host=element('Preview');host.replaceChildren();
      selected.forEach((item,index)=>{
        const card=document.createElement('div');card.className='reference-preview';
        const image=document.createElement(media?kind(item.file):'img');image.src=item.url;
        if(media){image.controls=true;image.preload='metadata';image.setAttribute('aria-label',item.file.name);}else image.alt=item.file.name;
        const name=document.createElement('span');name.textContent=item.file.name;name.title=item.file.name;
        const remove=document.createElement('button');remove.type='button';remove.className='text-button';
        remove.textContent='移除';remove.dataset.removeReference=String(index);remove.disabled=busy;
        remove.setAttribute('aria-label','移除 '+item.file.name);
        remove.onclick=()=>{if(busy)return;URL.revokeObjectURL(item.url);selected.splice(index,1);render();};
        card.append(image,name,remove);host.append(card);
      });
    }
    function setBusy(value){busy=value;inputs.forEach(input=>input.disabled=value||!enabled);render();}
    function setLimits(value={}){modelLimits=value;enabled=value?.enabled!==false;showLimits();inputs.forEach(input=>input.disabled=busy||!enabled);}
    function add(files,folder=false){
      const error=element('Error');error.textContent='';
      const incoming=[...files],images=folder?incoming.filter(file=>types.has(file.type)):incoming;
      if(selected.length+images.length>limits.max_count){error.textContent=`参考${media?'音视频':'图'}最多 ${limits.max_count} ${media?'个':'张'}，本次未添加。`;return;}
      for(const file of images){
        if(media?!kind(file):!types.has(file.type)){error.textContent=media?'请选择支持的音频或视频文件。':'请选择 JPG、PNG 或 WebP 图片。';return;}
        if(!file.size||file.size>limits.max_bytes){error.textContent=`素材为空或超过 ${Math.round(limits.max_bytes/1024/1024)} MB，本次未添加。`;return;}
      }
      selected.push(...images.map(file=>({file,url:URL.createObjectURL(file),id:null,platform:''})));
      if(folder&&incoming.length!==images.length)error.textContent=`已添加 ${images.length} 张图片，跳过 ${incoming.length-images.length} 个非 JPG、PNG、WebP 文件。`;
      render();
    }
    async function upload(remoteCount=0,onProgress=()=>{}){
      if(!enabled&&(selected.length||remoteCount))throw new Error('当前模型尚不支持参考图片，请先移除已选图片。');
      const platform=currentPlatform();await loadLimits(platform);
      if(selected.length+remoteCount>limits.max_count)throw new Error(media?`音视频合计最多 ${limits.max_count} 个。`:`本地图片和链接合计最多 ${limits.max_count} 张。`);
      if(selected.some(item=>!item.file.size||item.file.size>limits.max_bytes))throw new Error(`图片为空或超过 ${Math.round(limits.max_bytes/1024/1024)} MB。`);
      const wasBusy=busy;setBusy(true);
      try{
        for(let index=0;index<selected.length;index++){
          const item=selected[index];if(item.id&&item.platform===platform)continue;
          onProgress(`正在上传参考${media?'素材':'图'} ${index+1}/${selected.length}…`);
          try{
            const result=await request(platformPath(basePath,platform),{method:'POST',file:item.file,headers:media?{'X-File-Name':encodeURIComponent(item.file.name)}:{}});
            item.id=result.id;item.platform=platform;
          }catch(error){
            if([404,405].includes(error.status))throw new Error('素材上传功能尚未加载，请先重启 dola2 服务。');
            throw error;
          }
        }
        return selected.map(item=>item.id);
      }finally{setBusy(wasBusy);}
    }
    function clear(){selected.forEach(item=>URL.revokeObjectURL(item.url));selected=[];inputs.forEach(input=>input.value='');element('Error').textContent='';render();}
    inputs.forEach(input=>input.addEventListener('change',event=>{if(!busy&&enabled)add(event.target.files,input===element('Folder'));event.target.value='';}));
    loadLimits();
    return {upload,clear,setBusy,setLimits,count:()=>selected.length};
  }
  let admin,adminMedia;
  document.addEventListener('DOMContentLoaded',()=>{
    if(document.querySelector('#genRefFiles'))admin=create({api,query:q});
    if(document.querySelector('#genMediaFiles'))adminMedia=create({api,query:q,prefix:'genMedia',basePath:'/api/admin/reference-media',media:true});
  });
  return {create,upload:(...args)=>admin.upload(...args),uploadMedia:(...args)=>adminMedia?adminMedia.upload(...args):[],setBusy:value=>{admin?.setBusy(value);adminMedia?.setBusy(value);},setLimits:value=>admin?.setLimits(value),count:()=>admin?.count()||0,mediaCount:()=>adminMedia?.count()||0,clear:()=>{admin?.clear();adminMedia?.clear();}};
})();
