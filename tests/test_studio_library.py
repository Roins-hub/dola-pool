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
