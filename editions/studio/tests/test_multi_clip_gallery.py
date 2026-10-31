import unittest

from store import TaskStore


class MultiClipGalleryTests(unittest.TestCase):
    def setUp(self):
        self.store = TaskStore(':memory:')
        self.addCleanup(self.store._conn.close)

    def task(self, identity):
        self.store.create(identity, 'seedance-2.0', '原文', '16:9', 30, duration_profile='plugin-expert30')

    def test_gallery_has_one_entry_per_verified_clip_and_keeps_original_task_identity(self):
        self.task('dual')
        self.store.merge_clips('dual', [dict(clip_id='first',vid='v1',status='completed',video_url='/videos/first.mp4',actual_duration=15.1),
                                        dict(clip_id='second',vid='v2',status='completed',video_url='/videos/second.mp4',actual_duration=15.2)])
        self.store.update('dual',status='completed')
        rows=self.store.completed_videos()
        self.assertEqual(len(rows),2)
        self.assertEqual([row['id'] for row in rows],['dual','dual'])
        self.assertEqual([row['task_id'] for row in rows],['dual','dual'])
        self.assertEqual([row['clip_id'] for row in rows],['first','second'])
        self.assertEqual([row['sequence'] for row in rows],[1,2])
        self.assertEqual([row['actual_duration'] for row in rows],[15.1,15.2])
        self.assertEqual([row['video_url'] for row in rows],['/videos/first.mp4','/videos/second.mp4'])
        self.assertTrue(all(row['duration']==30 for row in rows))
        self.assertTrue(all('clips' not in row or not row['clips'] for row in rows))

    def test_partial_completed_clip_is_available_but_not_pending_or_deleted_clips(self):
        self.task('partial')
        self.store.merge_clips('partial',[dict(clip_id='ready',status='completed',video_url='/videos/ready.mp4',actual_duration=15),
                                         dict(clip_id='pending',status='processing',video_url='/videos/not-verified.mp4')])
        self.store.update('partial',status='failed',failure_code='partial_results')
        self.assertEqual([r['clip_id'] for r in self.store.completed_videos()],['ready'])
        self.store.soft_delete('partial','admin')
        self.assertEqual(self.store.completed_videos(),[])

    def test_old_single_video_keeps_its_existing_projection_and_entry_limit_is_honored(self):
        self.store.create('legacy','seedance-2.0','old','9:16',10)
        self.store.update('legacy',status='completed',video_url='/videos/old.mp4',actual_duration=10)
        old=self.store.completed_videos()[0]
        self.assertEqual((old['id'],old['duration'],old['actual_duration'],old['video_url']),
                         ('legacy',10,10,'/videos/old.mp4'))
        self.assertNotIn('clip_id',old)
        self.task('dual')
        self.store.merge_clips('dual',[dict(clip_id='a',status='completed',video_url='/videos/a.mp4'),
                                     dict(clip_id='b',status='completed',video_url='/videos/b.mp4')])
        self.store.update('dual',status='completed')
        self.assertEqual(len(self.store.completed_videos(limit=1)),1)


if __name__=='__main__': unittest.main()
