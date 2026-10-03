import asyncio
import base64
import io
import json
import re
import subprocess
import zipfile
from pathlib import Path
from types import SimpleNamespace
import pytest
from starlette.datastructures import FormData
from PIL import Image
import server
from store import TaskStore
from test_studio_batch_progress import studio
from ai_preflight import record_preflight_result


def folder_script(files):
    html = (Path(__file__).resolve().parents[1] / 'web/index.html').read_text()
    assert '// folder-parser-start' in html
    script = html.split('// folder-parser-start', 1)[1].split('// folder-parser-end', 1)[0]
    payload = json.dumps(files)
    js = script + '\nconst files=' + payload + ";for(const f of files){f.arrayBuffer=async()=>new TextEncoder().encode(f.content||'').buffer;}buildFolderGroups(files).then(r=>console.log(JSON.stringify(r))).catch(e=>{console.error(e.message);process.exit(2)});"
    return subprocess.run(['node', '-e', js], text=True, capture_output=True)


def test_folder_groups_one_txt_with_own_images():
    r = folder_script([{'name':'a.txt','webkitRelativePath':'root/B/a.txt','content':'Mèo\nNhảy'},
                       {'name':'b.png','webkitRelativePath':'root/B/b.png'},
                       {'name':'a.txt','webkitRelativePath':'root/A/a.txt','content':'Thuyền'},
                       {'name':'a.jpg','webkitRelativePath':'root/A/a.jpg'}])
    assert r.returncode == 0, r.stderr
    groups=json.loads(r.stdout)
    assert [g['name'] for g in groups]==['A','B']
    assert groups[1]['prompt']=='Mèo\nNhảy'
    assert groups[0]['image_indices']==[3] and groups[1]['image_indices']==[1]
    root=folder_script([{'name':'a.txt','webkitRelativePath':'root/a.txt','content':'Mèo'}])
    assert json.loads(root.stdout)[0]['name']=='.'


def test_ambiguous_group_rejected_before_submit():
    for files in [[{'name':'a.txt','webkitRelativePath':'root/a.txt','content':'A'}, {'name':'b.txt','webkitRelativePath':'root/b.txt','content':'B'}],
                  [{'name':'a.png','webkitRelativePath':'root/a.png'}],
                  [{'name':'a.txt','webkitRelativePath':'root/a.txt','content':'A'}, {'name':'a.mp3','webkitRelativePath':'root/a.mp3'}]]:
        r=folder_script(files)
        assert r.returncode == 2


def image(color):
    b=io.BytesIO();Image.new('RGB',(12,12),color).save(b,format='PNG')
    return 'data:image/png;base64,'+base64.b64encode(b.getvalue()).decode()

@pytest.mark.asyncio
async def test_batch_reference_inputs_owned_per_job(studio):
    import hashlib
    session=hashlib.sha256(b'test').hexdigest()[:24]
    entries=[{'prompt':p,'preflight_token':record_preflight_result(session,p,{'status':'allow'})['token'], 'reference_indices':[i]} for i,p in enumerate(['cat','boat'])]
    form=FormData([('batch',json.dumps({'request_id':'folder-test','prompts':entries,'copies':2,'duration':15})),('images',image('red')),('images',image('blue'))])
    async def getform(): return form
    r=await server.admin_create_batch(SimpleNamespace(form=getform),'test')
    rows=studio[0].batch_tasks(r['batch_id'])
    refs=[json.loads(r['reference_images']) for r in rows]
    assert all(len(r)==1 for r in refs) and len({r[0] for r in refs})==4
    assert Path(refs[0][0]).read_bytes()==Path(refs[1][0]).read_bytes()
    assert Path(refs[0][0]).read_bytes()!=Path(refs[2][0]).read_bytes()
    await asyncio.sleep(0)


def test_zip_names_cannot_escape_root(tmp_path, monkeypatch):
    media=tmp_path/'media';media.mkdir();(media/'video.mp4').write_bytes(b'fixture')
    outside=tmp_path/'outside.mp4';outside.write_bytes(b'private')
    (media/'escape.mp4').symlink_to(outside)
    monkeypatch.setattr(server.config,'DOWNLOAD_DIR',str(media))
    rows=[{'id':'../../bad','batch_index':1,'status':'completed','video_url':'/videos/video.mp4'},
          {'id':'escape','batch_index':2,'status':'completed','video_url':'/videos/escape.mp4'}]
    path=server._build_batch_zip(rows)
    try:
        with zipfile.ZipFile(path) as z:
            assert all('/' not in name and '..' not in name for name in z.namelist())
            assert z.read(next(n for n in z.namelist() if n.endswith('.mp4')))==b'fixture'
            assert b'private' not in z.read('ket-qua.json')
            assert json.loads(z.read('ket-qua.json'))['skipped'][0]['id']=='escape'
    finally: Path(path).unlink()


def test_zip_missing_deleted_video_reports_skipped(tmp_path, monkeypatch):
    monkeypatch.setattr(server.config,'DOWNLOAD_DIR',str(tmp_path))
    path=server._build_batch_zip([{'id':'gone','batch_index':1,'status':'completed','video_url':'/videos/missing.mp4'}])
    try:
        with zipfile.ZipFile(path) as z: assert json.loads(z.read('ket-qua.json'))['skipped'][0]['id']=='gone'
    finally: Path(path).unlink()


def test_export_path_rejected_if_unwritable(tmp_path, monkeypatch):
    with pytest.raises(ValueError): server._validate_export_directory('relative/folder')
    def denied(*a,**kw): raise PermissionError('fixture')
    monkeypatch.setattr(server.tempfile,'NamedTemporaryFile',denied)
    with pytest.raises(ValueError): server._validate_export_directory(str(tmp_path))

@pytest.mark.asyncio
@pytest.mark.parametrize('indices', [[-1], [2], [0,0], [True]])
async def test_invalid_reference_indices_never_create_job(studio, indices):
    form=FormData([('batch',json.dumps({'request_id':'invalid-index','prompts':[{'prompt':'cat','reference_indices':indices}]})),('images',image('red'))])
    async def getform(): return form
    with pytest.raises(server.HTTPException) as e:
        await server.admin_create_batch(SimpleNamespace(form=getform),'test')
    assert e.value.status_code==422 and not studio[0].recent_tasks(50)


def test_export_copy_keeps_source_and_settings_survive_restart(tmp_path, monkeypatch):
    media=tmp_path/'media';media.mkdir();source=media/'video.mp4';source.write_bytes(b'fixture-video')
    output=tmp_path/'export';output.mkdir()
    store=TaskStore(str(tmp_path/'tasks.db'));store.create('video_a','seedance-2.5','cat','16:9',15)
    store.update('video_a',status='completed',video_url='/videos/video.mp4')
    store.set_studio_option('export_directory',str(output))
    store=TaskStore(str(tmp_path/'tasks.db'))
    monkeypatch.setattr(server,'store',store);monkeypatch.setattr(server.config,'DOWNLOAD_DIR',str(media))
    server._copy_video_export('video_a')
    assert source.read_bytes()==b'fixture-video'
    assert next(output.glob('*.mp4')).read_bytes()==b'fixture-video'
    assert store.get('video_a')['video_url']=='/videos/video.mp4'


@pytest.mark.asyncio
async def test_open_output_rejects_tunnel_before_any_command(monkeypatch):
    monkeypatch.setattr(server,'_admin_auth',lambda _:None)
    def forbidden(*a,**kw): raise AssertionError('No command should run')
    monkeypatch.setattr(server.subprocess,'run',forbidden)
    request=SimpleNamespace(client=SimpleNamespace(host='127.0.0.1'),url=SimpleNamespace(hostname='public.example.com'),headers={})
    with pytest.raises(server.HTTPException) as e: await server.admin_open_output(request,x_admin_key=None)
    assert e.value.status_code==403
