import tempfile
import unittest
from pathlib import Path
import xml.etree.ElementTree as ET
import video_engine as engine


class OriginalXMLTests(unittest.TestCase):
    def test_nominal_rate_used_instead_of_variable_average(self):
        from unittest.mock import patch
        from types import SimpleNamespace
        source = dict(path='phone.mp4', fps=29.1, audio=True)
        metadata = b'Stream #0:0: Video: h264, 29.10 fps, 30 tbr, 90k tbn\nStream #0:1: Audio: aac, 48000 Hz, stereo, fltp'
        engine.imageio_ffmpeg.get_ffmpeg_exe()  # Resolve runtime before mocking subprocess.
        with patch.object(engine.subprocess, 'run', return_value=SimpleNamespace(stderr=metadata)):
            engine.source_xml_metadata(source)
        self.assertEqual(source['xml_fps'], 30)
        self.assertEqual(source['fps'], 29.1)
        self.assertEqual(source['audio_channels'], 2)
        self.assertTrue(source['xml_timing_approximate'])

    def test_phone_average_rate_and_long_offset(self):
        self.assertEqual(engine.xml_fps(29.99), 30)
        self.assertEqual(engine.xml_fps(30.01), 30)
        self.assertAlmostEqual(engine.xml_fps(29.97), 30000 / 1001)
        with self.assertRaises(ValueError):
            engine.xml_fps(27.4)
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as folder:
            target = Path(folder) / 'timeline.xml'
            clip = dict(name='phone', frames=60, source_start_seconds=205.25,
                        source_end_seconds=207.25,
                        source=dict(path=str(Path(folder)/'original.mp4'), fps=29.99,
                                    duration=300, audio=False))
            engine.write_xml(target, [clip], engine.DEFAULT)
            video = ET.parse(target).find('.//sequence/media/video/track/clipitem')
            self.assertEqual(video.findtext('rate/timebase'), '30')
            self.assertEqual(video.findtext('rate/ntsc'), 'FALSE')
            self.assertEqual(video.findtext('in'), str(round(205.25 * 30)))
            self.assertEqual(video.findtext('out'), str(round(207.25 * 30)))
            self.assertEqual(video.findtext('file/rate/ntsc'), 'FALSE')

    def test_original_paths_rates_offsets_and_audio(self):
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as folder:
            root = Path(folder)
            clips = []
            for index, (fps, audio, channels) in enumerate([(25, False, 0), (29.97, True, 1), (60, True, 2)]):
                source = root / ('źródło % & ' + str(index) + '.mov')
                source.write_bytes(b'original')
                clips.append(dict(name=source.name, frames=60,
                                  source_start_seconds=2, source_end_seconds=4,
                                  source=dict(path=str(source), fps=fps, duration=10,
                                              audio=audio, audio_channels=channels,
                                              metadata=dict(source_size=(3840, 2160)))))
            target = root / 'timeline.xml'
            engine.write_xml(target, clips, engine.DEFAULT)
            xml = ET.parse(target)
            videos = xml.findall('.//sequence/media/video/track/clipitem')
            self.assertEqual([v.findtext('start') for v in videos], ['0', '60', '120'])
            self.assertEqual([v.findtext('in') for v in videos], ['60', '60', '60'])
            self.assertEqual([v.findtext('out') for v in videos], ['120', '120', '120'])
            self.assertEqual(videos[1].findtext('file/rate/ntsc'), 'TRUE')
            self.assertEqual(videos[1].findtext('rate/timebase'), '30')
            audio_clips = xml.findall('.//sequence/media/audio/track/clipitem')
            self.assertEqual(len(audio_clips), 2)  # One native stream for mono OR stereo.
            declarations = {f.get('id'): f for f in xml.findall('.//file') if f.find('pathurl') is not None}
            for audio_clip in audio_clips:
                source = declarations[audio_clip.find('file').get('id')]
                self.assertTrue(source.findtext('pathurl').startswith('file://localhost/'))
                self.assertEqual(source.findtext('timecode/string'), '00:00:00:00')
                self.assertEqual(audio_clip.findtext('sourcetrack/trackindex'), '1')
                self.assertEqual(audio_clip.findtext('filter/effect/parameter/value'), '1')
                for link in audio_clip.findall('link'):
                    self.assertIsNotNone(xml.find(".//clipitem[@id='"+link.findtext('linkclipref')+"']"))
            self.assertEqual(videos[1].findtext('file/media/audio/channelcount'), '1')
            self.assertEqual(videos[2].findtext('file/media/audio/channelcount'), '2')
            for clip, video in zip(clips, videos):
                self.assertEqual(video.findtext('file/pathurl'), Path(clip['source']['path']).resolve().as_uri().replace('file:///', 'file://localhost/', 1))
                self.assertEqual(video.findtext('file/media/video/samplecharacteristics/width'), '3840')
            self.assertNotIn('/media/', target.read_text(encoding='utf-8'))


if __name__ == '__main__':
    unittest.main()
