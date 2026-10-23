import hashlib
from pathlib import Path
import tempfile
import unittest
import video_engine as engine
import review_features as features


class Review11(unittest.TestCase):
    def test_overlap_and_boundaries_including_rejected(self):
        state = dict(files=[dict(id='f', path='video.mp4', duration=10)], candidates=[
            dict(id='a', file_id='f', name='a', start=1., end=3., decision='accepted'),
            dict(id='b', file_id='f', name='b', start=3., end=5., decision='rejected')])
        with self.assertRaises(ValueError): features.changed_range(state, 'a', 'end', .5)
        self.assertEqual(features.changed_range(state, 'a', 'start', -.5)['start'], .5)
        with self.assertRaises(ValueError): features.changed_range(state, 'a', 'start', -2)

    def test_chronology_interleaves_cameras_and_timezone(self):
        state = dict(files=[dict(id='a', path='Z.mp4', recorded_at='2025-01-01T12:00:00+01:00'),
                            dict(id='b', path='A.mp4', recorded_at='2025-01-01T11:00:05Z')])
        candidates = [dict(id='a2',file_id='a',start=10), dict(id='b',file_id='b',start=0), dict(id='a1',file_id='a',start=0)]
        self.assertEqual([c['id'] for c in features.ordered(state,candidates)], ['a1','b','a2'])
        state['recording_times'] = {'b':'2025-01-01T12:00:00Z'}
        self.assertEqual([c['id'] for c in features.ordered(state,candidates)], ['a1','a2','b'])

    def test_trim_persists_and_export_uses_adjusted_source(self):
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as tmp:
            root=Path(tmp); source=root/'sources';source.mkdir()
            original=source/'clip.mp4'
            engine.ffmpeg(['-y','-f','lavfi','-i','testsrc2=size=320x180:rate=25:duration=6',
                           '-c:v','libx264',original])
            before=hashlib.sha256(original.read_bytes()).hexdigest()
            project=root/'project'
            state=engine.analyze(source,project,engine.DEFAULT|dict(width=320,height=180,min_score=0))
            c=state['candidates'][0]
            engine.record_decision(project,c['id'],'accepted')
            engine.trim(project,c['id'],'start',.5)
            updated=engine.load_project(project)['candidates'][0]
            self.assertEqual(updated['start'], c['start']+.5)
            self.assertEqual(updated['decision'],'accepted')
            self.assertNotEqual(updated['preview'],c['preview'])
            self.assertTrue((project/updated['preview']).is_file())
            out=engine.export(project)
            result=engine.read_json(out/'manifest.json')
            start=result['clips'][0]['source_start_seconds']
            self.assertGreaterEqual(start,updated['start'])
            self.assertLess(start-updated['start'],1/result['config']['timeline_fps'])
            self.assertEqual(hashlib.sha256(original.read_bytes()).hexdigest(),before)

    def test_configuration_checks(self):
        with self.assertRaises(ValueError): engine.config(engine.DEFAULT|dict(min_clip_seconds=10,max_clip_seconds=2))
        with self.assertRaises(ValueError): engine.config(engine.DEFAULT|dict(categories=['invented']))


if __name__=='__main__': unittest.main()
