import hashlib
import io
import json
from pathlib import Path
import re
import tempfile
import time
import unittest
import application as app
import video_engine as engine


class ApplicationTest(unittest.TestCase):
    def test_close_stops_worker_and_preserves_project(self):
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as tmp:
            base=Path(tmp);source=base/'originals';source.mkdir()
            engine.ffmpeg(['-y','-f','lavfi','-i','testsrc2=size=320x180:rate=25:duration=5',
                           '-c:v','libx264',source/'clip.mp4'])
            application=app.Application(base/'data')
            application.start(str(source))
            process=application.process
            project=application.project()
            application.close()
            self.assertIsNotNone(process.poll())
            self.assertIsNone(application.process)
            self.assertTrue(project.parent.exists())
            self.assertTrue((source/'clip.mp4').exists())
            application.close()  # Idempotent when server finally closes.

    def test_stop_analysis_and_cleanup(self):
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as tmp:
            base=Path(tmp); source=base/'sources';source.mkdir()
            engine.ffmpeg(['-y','-f','lavfi','-i','testsrc2=size=320x180:rate=25:duration=30',
                           '-c:v','libx264',source/'test.mp4'])
            application=app.Application(base/'data')
            application.start(str(source))
            application.stop()
            self.assertIsNone(application.process)
            self.assertIsNone(application.operation)
            project=application.project()
            if (project/'project.json').exists():
                self.assertEqual(engine.read_json(project/'project.json')['status'],'ready')
            plan=application.prepare_cleanup();application.cleanup(plan['ticket'])
            self.assertTrue((source/'test.mp4').is_file())

    def wait_idle(self, application):
        deadline=time.monotonic()+60
        while application.operation and time.monotonic()<deadline:
            time.sleep(.05)
        self.assertIsNone(application.operation)
        self.assertFalse(application.error,application.error)

    def test_full_workflow_and_cleanup_protects_originals(self):
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as tmp:
            base=Path(tmp); sources=base/'originals'; sources.mkdir()
            original=sources/'test.mp4'
            engine.ffmpeg(['-y','-f','lavfi','-i','testsrc2=size=320x180:rate=25:duration=2.5',
                           '-c:v','libx264','-pix_fmt','yuv420p',original])
            before=hashlib.sha256(original.read_bytes()).hexdigest()
            application=app.Application(base/'app-data')
            with self.assertRaises(ValueError): application.start(str(application.root))
            application.start(str(sources))
            with self.assertRaises(ValueError): application.prepare_cleanup()
            self.wait_idle(application)
            state=application.state()
            self.assertTrue(state['candidates'])
            candidate=state['candidates'][0]
            application.decision(candidate['id'],'accepted')
            self.assertEqual(application.state()['candidates'][0]['decision'],'accepted')
            application.trim(candidate['id'], 'start', .5)
            self.wait_idle(application)
            self.assertEqual(application.state()['candidates'][0]['start'],candidate['start']+.5)
            # Old saved projects may contain a limit; it must no longer restrict export.
            project=application.project()
            old=engine.read_json(project/'project.json');old['config']['max_export_seconds']=.1
            engine.save_json(project/'project.json',old)
            application.chronology({f['id']:'2025-11-08T18:00:00+01:00' for f in old['files']})
            application.start_export(); self.wait_idle(application)
            state=application.state()
            self.assertTrue(state['export'])
            self.assertTrue(application.asset(state['export']['xml']).is_file())
            self.assertIsNone(application.asset('/asset/'+state['selected']+'/../../test.mp4'))
            restarted=app.Application(base/'app-data')
            self.assertEqual(restarted.state()['selected'],state['selected'])
            self.assertTrue(restarted.state()['export'])
            completed_export=state['export']['folder']
            application.start_export()
            application.close()
            self.assertIsNone(application.process)
            self.assertEqual(application.state()['export']['folder'],completed_export)
            self.assertFalse((application.project()/'export.lock').exists())
            plan=application.prepare_cleanup()
            self.assertTrue(plan['has_exports'])
            self.assertGreater(plan['files'],4)
            with self.assertRaises(ValueError): application.cleanup('wrong-ticket')
            project=application.project()
            (project/'new-file.txt').write_text('changed')
            with self.assertRaises(ValueError): application.cleanup(plan['ticket'])
            plan=application.prepare_cleanup()
            application.cleanup(plan['ticket'])
            self.assertFalse(project.exists())
            self.assertIsNone(application.state()['selected'])
            self.assertEqual(hashlib.sha256(original.read_bytes()).hexdigest(),before)

    def test_export_longer_than_120_seconds(self):
        from unittest.mock import patch
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as tmp:
            root=Path(tmp);project=root/'project';project.mkdir();original=root/'original.mp4';original.write_bytes(b'test-source')
            stat=original.stat()
            state=dict(config=engine.DEFAULT|{'max_export_seconds':120},
                       files=[dict(id='a',path=str(original.resolve()),size=stat.st_size,mtime_ns=stat.st_mtime_ns,audio=False,fps=30,duration=135)],
                       candidates=[dict(id='clip',file_id='a',name='long',start=0,end=135,decision='accepted')])
            # Rendering is covered by the real short-film test; here verify a long
            # timeline and inclusion of every accepted frame without encoding 135 s.
            with patch.object(engine,'render_segment') as render, patch.object(engine,'ffmpeg'), patch.object(engine.media_timing,'video_timing',return_value=dict(frame_count=4050,duration=135,constant_rate=True)):
                out=engine.export(project,state)
                self.assertEqual(render.call_args.args[3],4050)
                self.assertEqual(engine.read_json(out/'manifest.json')['clips'][0]['frames'],4050)

    def fake_job(self, application, source):
        import uuid
        job_id=uuid.uuid4().hex; root=application.jobs/job_id
        (root/'project').mkdir(parents=True)
        engine.save_json(root/'.lpx-ai-video-review-owned.json',dict(application='lpx-ai-video-review',id=job_id))
        application.registry['jobs'].append(dict(id=job_id,source=str(source),name='test',total=0,export=None))
        application.registry['selected']=job_id
        return root

    def test_cleanup_guards(self):
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as tmp:
            base=Path(tmp); source=base/'source'; source.mkdir()
            application=app.Application(base/'data')
            first=self.fake_job(application,source)
            other=self.fake_job(application,source)
            application.registry['selected']=first.name
            engine.save_json(first/'project'/'project.json',dict(files=[dict(path=str(first/'keep.mp4'))]))
            with self.assertRaises(ValueError): application.prepare_cleanup()
            engine.save_json(first/'project'/'project.json',dict(files=[]))
            engine.save_json(first/'.lpx-ai-video-review-owned.json',dict(application='other',id=first.name))
            with self.assertRaises(ValueError): application.prepare_cleanup()
            engine.save_json(first/'.lpx-ai-video-review-owned.json',dict(application='lpx-ai-video-review',id=first.name))
            plan=application.prepare_cleanup()
            application.cleanup(plan['ticket'])
            self.assertTrue(other.exists())
            self.assertTrue(source.exists())

    def test_http_handlers_without_network_client(self):
        # Exercise real handlers with byte streams: this environment blocks outbound
        # loopback connections, so there is no claim of a browser end-to-end test.
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as tmp:
            application=app.Application(Path(tmp)/'data'); server=app.make_server(application,0)
            try:
                def request(path,method='GET',payload=None,token='',host=None):
                    handler=server.RequestHandlerClass.__new__(server.RequestHandlerClass)
                    handler.server=server;handler.client_address=('127.0.0.1',1234)
                    handler.request_version='HTTP/1.1';handler.requestline=method+' '+path+' HTTP/1.1';handler.command=method
                    handler.path=path;body=json.dumps(payload or {}).encode()
                    handler.headers={'Host':host or f'127.0.0.1:{server.server_port}',
                                     'Content-Length':str(len(body)),'X-Review-Token':token}
                    handler.rfile=io.BytesIO(body);handler.wfile=io.BytesIO()
                    getattr(handler,'do_'+method)()
                    return handler.wfile.getvalue()
                page=request('/')
                self.assertIn(b'200 OK',page)
                token=re.search(rb"const TOKEN='([^']+)'",page).group(1).decode()
                self.assertIn(b'403 Forbidden',request('/api/cleanup/confirm','POST',{'ticket':'x'}))
                self.assertIn(b'403 Forbidden',request('/api/state',host='evil.example'))
                self.assertIn(b'200 OK',request('/api/state'))
                self.assertIn(b'400 Bad Request',request('/api/analyze','POST',{'folder':'__missing__'},token))
                self.assertIn(b'404 Not Found',request('/../../original.mp4'))
                self.assertIn(b'400 Bad Request',request('/api/cleanup/confirm','POST',{'ticket':'x'},token))
                import threading
                from unittest.mock import patch
                stopped=threading.Event()
                with patch.object(server,'shutdown',stopped.set):
                    self.assertIn(b'200 OK',request('/api/shutdown','POST',{},token))
                    self.assertTrue(stopped.wait(2))
            finally: server.server_close()


if __name__=='__main__': unittest.main(verbosity=2)
