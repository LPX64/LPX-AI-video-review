import tempfile
import unittest
from pathlib import Path
import xml.etree.ElementTree as ET
import video_engine as engine
import media_timing


class MixedRates(unittest.TestCase):
    def test_mixed_sources_near_eof(self):
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as tmp:
            root=Path(tmp); project=root/'project';project.mkdir()
            files=[];candidates=[]
            for index, fps in enumerate(['25','30','60000/1001']):
                path=root/f'clip-{index}.mp4'
                engine.ffmpeg(['-y','-f','lavfi','-i',f'testsrc2=size=320x180:rate={fps}:duration=3',
                               '-f','lavfi','-i','sine=frequency=440:duration=3',
                               '-c:v','libx264','-c:a','aac','-ac','2','-shortest',path])
                meta=engine.probe(path);stat=path.stat()
                files.append(dict(id=str(index),path=str(path),fps=meta['fps'],duration=meta['duration'],
                                  audio=True,size=stat.st_size,mtime_ns=stat.st_mtime_ns,metadata=meta))
                candidates.append(dict(id=str(index),file_id=str(index),name=path.name,start=2.51,
                                       end=meta['duration'],decision='accepted'))
            state=dict(files=files,candidates=candidates,config=engine.DEFAULT|dict(width=320,height=180))
            out=engine.export(project,state)
            manifest=engine.read_json(out/'manifest.json')
            self.assertEqual(manifest['config']['timeline_fps'],30)
            xml=ET.parse(out/'timeline.xml')
            video=xml.findall('.//sequence/media/video/track/clipitem')
            audio=xml.findall('.//sequence/media/audio/track/clipitem')
            position=0
            for c,v,a in zip(manifest['clips'],video,audio):
                self.assertLessEqual(c['source_end_seconds'],c['source']['video_duration']+1e-8)
                self.assertEqual(int(v.findtext('start')),position)
                self.assertEqual(int(v.findtext('in')),round(c['source_start_seconds']*30))
                self.assertEqual(int(v.findtext('out'))-int(v.findtext('in')),c['frames'])
                self.assertEqual(v.findtext('rate/timebase'),'30')
                self.assertEqual(v.findtext('in'),a.findtext('in'))
                decoded,_=engine.imageio_ffmpeg.count_frames_and_secs(c['path'])
                self.assertEqual(decoded,c['frames'])
                position+=c['frames']
            self.assertEqual([v.findtext('file/rate/timebase') for v in video],['25','30','60'])
            self.assertEqual(video[-1].findtext('file/rate/ntsc'),'TRUE')

    def test_no_rounding_past_eof(self):
        first,last=media_timing.plan_range(29.25,36.99,2216/(60000/1001),60000/1001)
        self.assertEqual(last,2216)
        self.assertGreater(last,first)
        with self.assertRaises(ValueError):media_timing.plan_range(3,4,2,30)

