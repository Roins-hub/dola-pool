from test_studio_batch_progress import studio
import server
import pytest

@pytest.mark.asyncio
async def test_library_search_pagination_and_batch_filter(studio):
 store,_=studio
 for i in range(121):
  store.create(str(i),'seedance-2.5','mèo '+str(i),'9:16',30)
  store.update(str(i),status='completed',batch_id='one' if i%2 else 'two')
 r=await server.admin_tasks(limit=20,offset=100,search='mèo',status='completed',batch_id='',x_admin_key='test')
 assert len(r['tasks'])==20 and r['total']==121
 r=await server.admin_tasks(limit=20,offset=0,search='mèo 120',status='completed',batch_id='two',x_admin_key='test')
 assert len(r['tasks'])==1 and r['tasks'][0]['id']=='120'
 r=await server.admin_tasks(limit=20,offset=0,search='%',status='all',batch_id='',x_admin_key='test')
 assert r['total']==0

@pytest.mark.asyncio
async def test_library_titles_dates_and_rename_preserve_job(studio):
 store,calls=studio
 store.create('old','seedance-2.5','prompt gốc','9:16',30)
 store.update('old',created_at=100,status='processing',conversation_id='same-conversation',account='same-account',deadline_at=1900)
 store.create('new','seedance-2.5','prompt khác','9:16',15)
 store.update('new',created_at=200,status='completed')
 before=store.get('old')
 await server.admin_name_video('old',server.VideoNameRequest(title='  Mèo giao pizza  '),x_admin_key='test')
 after=store.get('old')
 assert after['title']=='Mèo giao pizza'
 assert {k:v for k,v in before.items() if k!='title'}=={k:v for k,v in after.items() if k!='title'}
 assert not calls
 r=await server.admin_tasks(search='giao pizza',created_after=100,created_before=200,x_admin_key='test')
 assert r['total']==1 and r['tasks'][0]['id']=='old'
 r=await server.admin_tasks(created_after=200,created_before=300,x_admin_key='test')
 assert r['total']==1 and r['tasks'][0]['id']=='new'
 assert (await server.admin_get_task('old',x_admin_key='test'))['title']=='Mèo giao pizza'
 with pytest.raises(server.HTTPException) as e:
  await server.admin_tasks(created_after=200,created_before=100,x_admin_key='test')
 assert e.value.status_code==400
 with pytest.raises(server.HTTPException) as e:
  await server.admin_name_video('missing',server.VideoNameRequest(title='Tên'),x_admin_key='test')
 assert e.value.status_code==404
 await server.admin_name_video('old',server.VideoNameRequest(title=''),x_admin_key='test')
 assert store.get('old')['title']==''


def test_title_migration_keeps_existing_rows(tmp_path):
 import sqlite3
 from store import TaskStore
 path=tmp_path/'old.db'
 with sqlite3.connect(path) as conn:
  conn.execute('CREATE TABLE tasks (id TEXT PRIMARY KEY, model TEXT, prompt TEXT, ratio TEXT, duration INTEGER, status TEXT, video_url TEXT, error TEXT, created_at REAL, updated_at REAL)')
  conn.execute("INSERT INTO tasks VALUES ('legacy','seedance-2.5','gốc','9:16',30,'completed',NULL,NULL,100,101)")
 store=TaskStore(str(path))
 assert store.get('legacy')['prompt']=='gốc' and store.get('legacy')['title']==''
 assert store.library_tasks(search='gốc')[1]==1
