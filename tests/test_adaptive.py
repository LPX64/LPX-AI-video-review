import tempfile
import unittest
from pathlib import Path
import video_engine as app


def sample(change=.01, quality=.9):
    return dict(quality=quality, residual=change, motion=.15, dx=0, dy=0)


class Adaptive(unittest.TestCase):
    def test_short_intervening_run_does_not_create_overlap(self):
        import random
        rng = random.Random(42)
        for _ in range(100):
            samples = [sample(rng.choice([.01,.05,.15,.2]), rng.choice([.1,.8,.9])) for _ in range(100)]
            proposals = app.pick_windows(samples, 50, app.DEFAULT | dict(candidates_per_file=100))
            for a,b in zip(proposals, proposals[1:]):
                self.assertLessEqual(a['end'], b['start'])

    def test_brief_and_sustained_events(self):
        samples = [sample()] * 40 + [sample(.2)] * 4 + [sample()] * 40 + [sample(.2)] * 40 + [sample()] * 40
        config = app.DEFAULT | dict(sample_fps=4, candidates_per_file=100)
        proposals = app.pick_windows(samples, len(samples)/4, config)
        events = [x for x in proposals if x['reason'] == 'zmiana obrazu']
        self.assertEqual(len(events), 2)
        lengths = [x['end']-x['start'] for x in events]
        self.assertLess(lengths[0], 4)
        self.assertGreater(lengths[1], 9)
        self.assertLessEqual(events[0]['start'], 10)
        self.assertGreaterEqual(events[0]['end'], 11)
        for a, b in zip(proposals, proposals[1:]):
            self.assertLessEqual(a['end'], b['start'])

    def test_bounds_black_static_and_legacy_config(self):
        self.assertEqual(app.pick_windows([sample(quality=0)]*100, 50, app.DEFAULT), [])
        short = app.pick_windows([sample()]*2, .8, app.DEFAULT)
        self.assertEqual(len(short), 1)
        self.assertEqual(short[0]['end'], .8)
        long = app.pick_windows([sample()]*240, 120, app.DEFAULT)
        self.assertTrue(long)
        self.assertTrue(all(1.5 <= x['end']-x['start'] <= 24 for x in long))
        self.assertGreater(long[0]['end']-long[0]['start'], 6)
        alternate = app.DEFAULT | dict(window_seconds=3, stride_seconds=1)
        self.assertEqual(long, app.pick_windows([sample()]*240, 120, alternate))

    def test_video_two_different_passages(self):
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as folder:
            root = Path(folder)
            source = root / 'sources'
            source.mkdir()
            video = source / 'short-and-long.mp4'
            app.ffmpeg(['-y', '-f', 'lavfi', '-i', 'testsrc2=size=320x180:rate=25:duration=18',
                        '-vf', "drawbox=color=black:t=fill:enable='lt(t,2)+between(t,3,5)+gte(t,14)'",
                        '-c:v', 'libx264', '-pix_fmt', 'yuv420p', video])
            c = app.DEFAULT | dict(sample_fps=4, width=320, height=180)
            project = root / 'project'
            state = app.analyze(source, project, c)
            self.assertFalse(state['errors'])
            lengths = [x['end']-x['start'] for x in state['candidates']]
            self.assertTrue(any(x < 4 for x in lengths), lengths)
            self.assertTrue(any(x > 6 for x in lengths), lengths)
            for candidate in state['candidates']:
                app.record_decision(project, candidate['id'], 'accepted')
            out = app.export(project)
            clips = app.read_json(out/'manifest.json')['clips']
            for clip in clips:
                frames, _ = app.imageio_ffmpeg.count_frames_and_secs(clip['path'])
                self.assertEqual(frames, clip['frames'])
            self.assertTrue((out/'timeline.xml').exists())


if __name__ == '__main__':
    unittest.main(verbosity=2)
