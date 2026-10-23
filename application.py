"""Single-user local application. All disposable files live in owned job folders."""
from __future__ import annotations
import argparse
import hashlib
import json
import mimetypes
import os
from pathlib import Path
import re
import secrets
import shutil
import subprocess
import sys
import threading
import time
import uuid
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse
import video_engine as engine


def bind_process_lifetime(process):
    """Windows closes this job on app exit, terminating only our worker tree."""
    if os.name != 'nt':
        return None
    import ctypes
    from ctypes import wintypes as w
    class Basic(ctypes.Structure):
        _fields_ = [('process_time',ctypes.c_longlong),('job_time',ctypes.c_longlong),
                    ('flags',w.DWORD),('minimum',ctypes.c_size_t),('maximum',ctypes.c_size_t),
                    ('active',w.DWORD),('affinity',ctypes.c_size_t),('priority',w.DWORD),('scheduling',w.DWORD)]
    class Extended(ctypes.Structure):
        _fields_ = [('basic',Basic),('io',ctypes.c_ulonglong*6),('process_memory',ctypes.c_size_t),
                    ('job_memory',ctypes.c_size_t),('peak_process',ctypes.c_size_t),('peak_job',ctypes.c_size_t)]
    kernel=ctypes.WinDLL('kernel32',use_last_error=True)
    kernel.CreateJobObjectW.argtypes=[ctypes.c_void_p,w.LPCWSTR]; kernel.CreateJobObjectW.restype=w.HANDLE
    kernel.SetInformationJobObject.argtypes=[w.HANDLE,ctypes.c_int,ctypes.c_void_p,w.DWORD]
    kernel.AssignProcessToJobObject.argtypes=[w.HANDLE,w.HANDLE]
    kernel.CloseHandle.argtypes=[w.HANDLE]
    handle=kernel.CreateJobObjectW(None,None)
    if not handle: raise ctypes.WinError(ctypes.get_last_error())
    info=Extended(); info.basic.flags=0x2000
    if not kernel.SetInformationJobObject(handle,9,ctypes.byref(info),ctypes.sizeof(info)) or not kernel.AssignProcessToJobObject(handle,w.HANDLE(int(process._handle))):
        error=ctypes.get_last_error();kernel.CloseHandle(handle);raise ctypes.WinError(error)
    return lambda: kernel.CloseHandle(handle)


def reject_links(path):
    path = Path(path)
    for part in [path, *path.parents]:
        if part.is_symlink() or (hasattr(part, 'is_junction') and part.is_junction()):
            raise ValueError('Folder aplikacji nie może być dowiązaniem ani junction.')


class Application:
    def __init__(self, data_dir):
        reject_links(data_dir)
        self.root = Path(data_dir).resolve()
        self.jobs = self.root / 'jobs'
        reject_links(self.jobs)
        self.jobs.mkdir(parents=True, exist_ok=True)
        self.lock = threading.RLock()
        self.process = None
        self.operation = None
        self.error = ''
        self.cleanup_ticket = None
        self.worker_lifetime = None
        self.worker_log = None
        self.registry_path = self.root / 'application.json'
        self.registry = engine.read_json(self.registry_path) if self.registry_path.exists() else {'selected': None, 'jobs': []}
        # A crashed process may leave an analyzing flag; no background process is
        # presumed alive after restarting this single-instance application.
        for job in self.registry['jobs']:
            project = self.project(job['id'])
            if job.pop('export_running',False):
                # The owned Windows job terminates worker processes on exit.
                # Recover the lock left by an interrupted managed export.
                (project/'export.lock').unlink(missing_ok=True)
                self.error = 'Poprzedni eksport został przerwany. Możesz wygenerować go ponownie lub wyczyścić projekt.'
            if (project/'project.json').exists():
                state = engine.read_json(project/'project.json')
                if state.get('status') == 'analyzing':
                    state.update(status='ready', interrupted=True)
                    engine.save_json(project/'project.json', state)
        self.save()

    def save(self):
        engine.save_json(self.registry_path, self.registry)

    def job(self, job_id=None):
        job_id = job_id or self.registry['selected']
        return next((x for x in self.registry['jobs'] if x['id'] == job_id), None)

    def owned(self, job_id):
        if not re.fullmatch(r'[0-9a-f]{32}', job_id or ''):
            raise ValueError('Nieprawidłowy identyfikator projektu.')
        path = self.jobs / job_id
        reject_links(path)
        resolved = path.resolve()
        if resolved.parent != self.jobs.resolve():
            raise ValueError('Projekt leży poza folderem aplikacji.')
        marker = engine.read_json(resolved / '.lpx-ai-video-review-owned.json')
        if marker != {'application': 'lpx-ai-video-review', 'id': job_id}:
            raise ValueError('Brak znacznika własności projektu. Czyszczenie zablokowane.')
        return resolved

    def project(self, job_id=None):
        job = self.job(job_id)
        if not job:
            raise ValueError('Najpierw wybierz folder i uruchom analizę.')
        return self.owned(job['id']) / 'project'

    def validate_source(self, folder):
        source = Path(folder).expanduser().resolve()
        if not source.is_dir():
            raise ValueError('Ten folder nie istnieje lub jest niedostępny.')
        if source == self.root or source.is_relative_to(self.root) or self.root.is_relative_to(source):
            raise ValueError('Folder nagrań musi być oddzielony od folderu danych aplikacji.')
        count = sum(p.is_file() and p.suffix.lower() in ('.mp4', '.mov') for p in source.rglob('*'))
        if not count:
            raise ValueError('W tym folderze i podfolderach nie znaleziono MP4 ani MOV.')
        return source, count

    def ensure_idle(self):
        if self.operation:
            raise ValueError('Najpierw zatrzymaj analizę lub poczekaj na zakończenie eksportu.')

    def select(self, job_id):
        with self.lock:
            self.ensure_idle()
            if not self.job(job_id):
                raise ValueError('Nie znaleziono projektu.')
            self.registry['selected'] = job_id
            self.error = ''
            self.save()

    def start(self, folder, profile='mixed', options=None):
        with self.lock:
            self.ensure_idle()
            if profile not in ('mixed', 'pov', 'phone'):
                raise ValueError('Nieprawidłowy profil.')
            source, total = self.validate_source(folder)
            settings = engine.config(Path(__file__).with_name('configs')/(profile+'.json'))
            options = options or {}
            if set(options) - {'min_clip_seconds','max_clip_seconds','categories','semantic_enabled'}:
                raise ValueError('Nieznane ustawienia analizy.')
            settings.update(options)
            settings = engine.config(settings)
            if settings['categories'] and not settings['semantic_enabled']:
                raise ValueError('Włącz model, aby filtrować kategorie.')
            if settings['semantic_enabled'] and not engine.semantic.available():
                raise ValueError('Zainstaluj lokalny model plikiem Zainstaluj-kategorie.cmd i uruchom ponownie aplikację.')
            job_id = uuid.uuid4().hex
            job_root = self.jobs / job_id
            job_root.mkdir()
            engine.save_json(job_root/'.lpx-ai-video-review-owned.json', {'application': 'lpx-ai-video-review', 'id': job_id})
            job = dict(id=job_id, name=source.name+' · '+time.strftime('%d.%m %H:%M'),
                       source=str(source), total=total, created=time.time(), export=None)
            self.registry['jobs'].append(job)
            self.registry['selected'] = job_id
            self.save()
            project = job_root/'project'
            cfg = job_root/'config.json'
            engine.save_json(cfg, settings)
            engine.config(cfg)
            command = [sys.executable, str(Path(engine.__file__).resolve()), 'analyze',
                       '--input', str(source), '--project', str(project), '--config', str(cfg)]
            self.error = ''
            self.operation = 'analysis'
            try:
                log = (job_root/'analysis.log').open('wb')
                self.worker_log = log
                flags = subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0
                self.process = subprocess.Popen(command, stdout=log, stderr=subprocess.STDOUT, creationflags=flags)
                process = self.process
                self.worker_lifetime = bind_process_lifetime(process)
            except Exception:
                if self.process and self.process.poll() is None:
                    self.process.terminate()
                    self.process.wait(timeout=15)
                self.process = None
                self.operation = None
                if 'log' in locals(): log.close()
                raise

            def finish():
                result = process.wait()
                log.close()
                with self.lock:
                    if self.process is process:
                        if self.worker_lifetime:
                            self.worker_lifetime(); self.worker_lifetime=None
                        self.process = None
                        self.operation = None
                        if result not in (0, 2):
                            self.error = 'Analiza została przerwana. Gotowe propozycje zachowano; szczegóły w analysis.log.'
                        if (project/'project.json').exists():
                            state = engine.read_json(project/'project.json')
                            if state.get('status') == 'analyzing':
                                state.update(status='ready', interrupted=True)
                                engine.save_json(project/'project.json', state)
            threading.Thread(target=finish, daemon=True).start()
            return {'id': job_id, 'total': total}

    def stop(self):
        with self.lock:
            if self.operation != 'analysis' or not self.process:
                raise ValueError('Analiza nie jest uruchomiona.')
            process = self.process
            # Stop this specific owned process and its decoder children only.
            if os.name == 'nt' and self.worker_lifetime:
                self.worker_lifetime(); self.worker_lifetime=None
            else:
                process.terminate()
            process.wait(timeout=15)
            if self.worker_log:
                self.worker_log.close(); self.worker_log=None
            if self.worker_lifetime:
                self.worker_lifetime(); self.worker_lifetime=None
            self.process = None
            self.operation = None
            self.error = ''
            path = self.project()/'project.json'
            if path.exists():
                state = engine.read_json(path)
                state.update(status='ready', analysis_stopped_by_user=True)
                engine.save_json(path, state)

    def decision(self, candidate_id, decision):
        with self.lock:
            if self.operation == 'export':
                raise ValueError('Poczekaj na zakończenie eksportu.')
            engine.record_decision(self.project(), candidate_id, decision)

    def trim(self, candidate_id, edge, delta):
        with self.lock:
            self.ensure_idle()
            project = self.project()
            engine.review_features.changed_range(engine.load_project(project), candidate_id, edge, delta)
            self.error = ''
            log = (project.parent/'trim.log').open('wb')
            try:
                process = subprocess.Popen([sys.executable, str(Path(engine.__file__).resolve()),
                    'trim', '--project', str(project), '--id', candidate_id, '--edge', edge, '--delta', str(delta)],
                    stdout=log, stderr=subprocess.STDOUT,
                    creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0)
                lifetime = bind_process_lifetime(process)
            except Exception:
                if 'process' in locals() and process.poll() is None:
                    process.terminate(); process.wait(timeout=15)
                log.close()
                raise
            self.process, self.worker_lifetime, self.worker_log = process, lifetime, log
            self.operation = 'trim'
            def finish():
                code = process.wait()
                with self.lock:
                    if self.process is not process: return
                    log.close()
                    if self.worker_lifetime: self.worker_lifetime()
                    self.process = self.worker_lifetime = self.worker_log = self.operation = None
                    if code:
                        self.error = (project.parent/'trim.log').read_text(encoding='utf-8', errors='replace')[-2000:]
            threading.Thread(target=finish, daemon=True).start()

    def chronology(self, values):
        with self.lock:
            self.ensure_idle()
            state = engine.load_project(self.project())
            ids = {f['id'] for f in state['files']}
            if not isinstance(values, dict) or set(values) - ids:
                raise ValueError('Nieznane nagranie.')
            for value in values.values(): engine.review_features.timestamp(value)
            engine.save_json(self.project()/'chronology.json', values)

    def start_export(self):
        with self.lock:
            self.ensure_idle()
            project = self.project()
            state = engine.load_project(project)
            # Original files may predate recording-date support. Read their
            # metadata once before export; never substitute file modification time.
            times = state.get('recording_times', {})
            for f in state['files']:
                if not times.get(f['id']) and not f.get('recorded_at'):
                    recorded = engine.probe(f['path']).get('recorded_at')
                    if recorded: times[f['id']] = recorded
            engine.save_json(project/'chronology.json', times)
            selected_ids = {x['file_id'] for x in state['candidates'] if x['decision']=='accepted'}
            missing = [f for f in state['files'] if f['id'] in selected_ids and not (times.get(f['id']) or f.get('recorded_at'))]
            if missing:
                raise ValueError('Nie można ustalić chronologii. W sekcji Chronologia wpisz datę i czas początku nagrania: '+', '.join(Path(f['path']).name for f in missing))
            if not any(x['decision']=='accepted' for x in state['candidates']):
                raise ValueError('Zaakceptuj przynajmniej jeden klip.')
            job = self.job()
            self.error = ''
            self.operation = 'export'
            engine.save_json(project/'export-progress.json', dict(message='Rozpoczynanie eksportu. Proszę czekać…'))
            existing={p.name for p in project.glob('export-*')}
            job['export_running']=True
            self.save()
            try:
                log=(project.parent/'export.log').open('wb')
                self.worker_log=log
                flags=subprocess.CREATE_NO_WINDOW if os.name=='nt' else 0
                process=subprocess.Popen([sys.executable,str(Path(engine.__file__).resolve()),'export','--project',str(project)],
                                         stdout=log,stderr=subprocess.STDOUT,creationflags=flags)
                self.process=process
                self.worker_lifetime=bind_process_lifetime(process)
            except Exception:
                if self.process and self.process.poll() is None:
                    self.process.terminate();self.process.wait(timeout=15)
                if 'log' in locals():log.close()
                self.process=None;self.operation=None;job.pop('export_running',None);self.save()
                raise
            def finish():
                result=process.wait();log.close()
                with self.lock:
                    if self.process is not process:
                        return
                    if self.worker_lifetime:
                        self.worker_lifetime();self.worker_lifetime=None
                    self.process=None;self.operation=None
                    complete=[p for p in project.glob('export-*') if p.name not in existing and (p/'complete.json').exists()]
                    if result==0 and complete:
                        job['export']=max(complete,key=lambda p:p.stat().st_mtime).name
                    else:
                        self.error='Eksport nie został ukończony. '+(project.parent/'export.log').read_text(encoding='utf-8',errors='replace')[-1200:]
                    job.pop('export_running',None)
                    (project/'export.lock').unlink(missing_ok=True)
                    self.save()
            threading.Thread(target=finish, daemon=True).start()

    def close(self):
        """Stop owned workers before shutting down; keep saved media and choices."""
        with self.lock:
            if self.operation == 'analysis' and self.process:
                self.stop()
            elif self.process:
                process = self.process
                if self.worker_lifetime:
                    self.worker_lifetime(); self.worker_lifetime = None
                elif process.poll() is None:
                    process.terminate()
                process.wait(timeout=15)
                if self.worker_log:
                    self.worker_log.close(); self.worker_log=None
                self.process=None
                self.operation=None
                job=self.job()
                if job:
                    job.pop('export_running',None)
                    (self.project()/'export.lock').unlink(missing_ok=True)
                self.save()

    def state(self):
        with self.lock:
            job = self.job()
            result = dict(jobs=self.registry['jobs'], selected=self.registry['selected'], operation=self.operation,
                          error=self.error, candidates=[], errors=[], analyzed_files=0, total_files=0,
                          source='', export=None, project_path=None)
            result.update(version='1.21', categories={k:v[0] for k,v in engine.semantic.CATEGORIES.items()},
                          model_available=engine.semantic.available(), files=[], recording_times={})
            if not job:
                return result
            project = self.project()
            result.update(source=job['source'], total_files=job['total'], project_path=str(project))
            if self.operation == 'export' and (project/'export-progress.json').exists():
                result['export_progress'] = engine.read_json(project/'export-progress.json')
            if (project/'project.json').exists():
                state = engine.load_project(project)
                result.update(candidates=state['candidates'], errors=state['errors'],
                              analyzed_files=len(state['files']), files=state['files'], recording_times=state.get('recording_times', {}))
            if job.get('export'):
                out = project/job['export']
                if (out/'complete.json').exists():
                    result['export'] = dict(folder=str(out), xml=f"/output/{job['id']}/timeline.xml",
                                            preview=f"/output/{job['id']}/preview.mp4")
            return result

    def asset(self, url):
        # No user-controlled filesystem path joins: map only known generated assets.
        for job in self.registry['jobs']:
            prefix = '/asset/'+job['id']+'/'
            if url.startswith(prefix):
                project = self.project(job['id'])
                if not (project/'project.json').exists():
                    return None
                for candidate in engine.load_project(project)['candidates']:
                    for key in ('thumbnail','preview'):
                        if url == prefix+candidate[key]:
                            path = project/candidate[key]
                            reject_links(path)
                            if path.resolve().is_relative_to(project.resolve()):
                                return path
            if job.get('export') and url in ('/output/'+job['id']+'/timeline.xml','/output/'+job['id']+'/preview.mp4'):
                project = self.project(job['id'])
                path = project/job['export']/url.rsplit('/',1)[1]
                reject_links(path)
                if path.resolve().is_relative_to(project.resolve()):
                    return path
        return None

    def inventory(self):
        self.ensure_idle()
        job = self.job()
        if not job:
            raise ValueError('Nie ma projektu do wyczyszczenia.')
        target = self.owned(job['id'])
        if (target/'project'/'export.lock').exists():
            raise ValueError('Eksport jest aktywny lub został przerwany. Zamknij proces eksportu i usuń pozostawiony export.lock przed czyszczeniem.')
        source = Path(job['source']).resolve()
        if source == target or source.is_relative_to(target) or target.is_relative_to(source):
            raise ValueError('Folder źródłowy koliduje z folderem do usunięcia.')
        paths = list(target.rglob('*'))
        for path in paths:
            reject_links(path)
            if not path.resolve().is_relative_to(target):
                raise ValueError('Plik poza zakresem czyszczenia.')
        project = target/'project'
        if (project/'project.json').exists():
            for file in engine.read_json(project/'project.json')['files']:
                original = Path(file['path']).resolve()
                if original == target or original.is_relative_to(target):
                    raise ValueError('Projekt zawiera oryginał w folderze wyników. Czyszczenie zablokowane.')
        files = [p for p in paths if p.is_file()]
        fingerprint = hashlib.sha256('\n'.join(sorted(f'{p.relative_to(target)}:{p.stat().st_size}:{p.stat().st_mtime_ns}' for p in files)).encode()).hexdigest()
        return dict(id=job['id'], name=job['name'], folder=str(target), files=len(files),
                    bytes=sum(p.stat().st_size for p in files), has_exports=any(p.name=='timeline.xml' for p in files),
                    fingerprint=fingerprint)

    def prepare_cleanup(self):
        with self.lock:
            info = self.inventory()
            ticket = secrets.token_urlsafe(24)
            self.cleanup_ticket = (ticket, time.monotonic()+180, info)
            return info | {'ticket': ticket}

    def cleanup(self, ticket):
        with self.lock:
            saved = self.cleanup_ticket
            if not saved or saved[0] != ticket or saved[1] < time.monotonic():
                raise ValueError('Potwierdzenie wygasło. Ponownie wybierz czyszczenie.')
            current = self.inventory()
            if current != saved[2]:
                raise ValueError('Pliki zmieniły się. Ponownie sprawdź zakres czyszczenia.')
            target = self.owned(current['id'])
            # Absolute target and every descendant were validated above; no shell.
            # Preserve ownership marker until last, allowing a safe retry if an
            # open media file makes Windows refuse part of the cleanup.
            marker = target/'.lpx-ai-video-review-owned.json'
            for child in target.iterdir():
                if child == marker:
                    continue
                reject_links(child)
                if not child.resolve().is_relative_to(target):
                    raise ValueError('Plik poza zakresem czyszczenia.')
                if child.is_dir():
                    shutil.rmtree(child)
                else:
                    child.unlink()
            marker.unlink()
            target.rmdir()
            self.registry['jobs'] = [j for j in self.registry['jobs'] if j['id'] != current['id']]
            self.registry['selected'] = self.registry['jobs'][-1]['id'] if self.registry['jobs'] else None
            self.cleanup_ticket = None
            self.error = ''
            self.save()
            return {'deleted_files': current['files']}


def choose_folder():
    if os.name != 'nt':
        raise ValueError('Wpisz ścieżkę folderu w polu tekstowym.')
    script = """Add-Type -AssemblyName System.Windows.Forms
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
$dialog = New-Object System.Windows.Forms.FolderBrowserDialog
$dialog.Description = 'Wybierz folder z nagraniami MP4 / MOV'
$dialog.ShowNewFolderButton = $false
if ($dialog.ShowDialog() -eq [System.Windows.Forms.DialogResult]::OK) { [Console]::Write($dialog.SelectedPath) }
$dialog.Dispose()
"""
    result = subprocess.run(['powershell.exe','-NoProfile','-STA','-Command',script], capture_output=True,
                            creationflags=subprocess.CREATE_NO_WINDOW)
    if result.returncode:
        raise ValueError('Nie udało się otworzyć okna folderów. Wklej ścieżkę ręcznie.')
    return result.stdout.decode('utf-8-sig').strip()


def make_server(app, port=8770):
    token = secrets.token_urlsafe(32)
    class Handler(BaseHTTPRequestHandler):
        def allowed_host(self):
            return self.headers.get('Host') in (f'127.0.0.1:{self.server.server_port}', f'localhost:{self.server.server_port}')

        def send(self, status, value, content_type='application/json; charset=utf-8'):
            data = value if isinstance(value, bytes) else json.dumps(value,ensure_ascii=False).encode()
            self.send_response(status)
            self.send_header('Content-Type',content_type)
            self.send_header('Content-Length',str(len(data)))
            self.send_header('Cache-Control','no-store')
            self.send_header('X-Content-Type-Options','nosniff')
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):
            if not self.allowed_host():
                self.send(403,{}); return
            route = urlparse(self.path).path
            try:
                if route == '/':
                    html=Path(__file__).with_name('application.html').read_text(encoding='utf-8').replace('__TOKEN__',token)
                    self.send(200,html.encode(),'text/html; charset=utf-8')
                elif route == '/icon.svg':
                    self.send(200,Path(__file__).with_name('icon.svg').read_bytes(),'image/svg+xml')
                elif route == '/api/state':
                    self.send(200,app.state())
                else:
                    with app.lock:
                        path = app.asset(route)
                        if not path or not path.is_file():
                            self.send(404,{}); return
                        # Keep the file open while streaming. Cleanup may be retried
                        # on Windows if the user has an actively playing file open.
                        stream = path.open('rb')
                    with stream:
                        size = os.fstat(stream.fileno()).st_size
                        start,end,status=0,size-1,200
                        request=self.headers.get('Range')
                        if request:
                            match=re.fullmatch(r'bytes=(\d*)-(\d*)',request)
                            if not match or not any(match.groups()):
                                self.send(416,{}); return
                            a,b=match.groups()
                            if a:
                                start=int(a); end=min(int(b),end) if b else end
                            else:
                                start=max(0,size-int(b))
                            if start>end or start>=size:
                                self.send(416,{}); return
                            status=206
                        self.send_response(status)
                        self.send_header('Content-Type',mimetypes.guess_type(path.name)[0] or 'application/octet-stream')
                        self.send_header('Content-Length',str(end-start+1))
                        self.send_header('Accept-Ranges','bytes')
                        self.send_header('Cache-Control','no-store')
                        if path.suffix=='.xml':
                            self.send_header('Content-Disposition','attachment; filename="timeline.xml"')
                        if status==206:
                            self.send_header('Content-Range',f'bytes {start}-{end}/{size}')
                        self.end_headers()
                        stream.seek(start)
                        remaining=end-start+1
                        while remaining:
                            chunk=stream.read(min(65536,remaining))
                            if not chunk: break
                            self.wfile.write(chunk); remaining-=len(chunk)
            except (BrokenPipeError,ConnectionResetError,ConnectionAbortedError):
                pass
            except (ValueError,OSError,KeyError) as exc:
                self.send(400,{'error':str(exc)})

        def do_POST(self):
            if not self.allowed_host() or self.headers.get('X-Review-Token') != token:
                self.send(403,{}); return
            try:
                length=int(self.headers.get('Content-Length','0'))
                if not 0<length<16384: raise ValueError('Nieprawidłowe żądanie.')
                data=json.loads(self.rfile.read(length))
                route=urlparse(self.path).path
                result={'ok':True}
                if route=='/api/folder': result={'folder':choose_folder()}
                elif route=='/api/analyze': result=app.start(data['folder'],data.get('profile','mixed'),data.get('options'))
                elif route=='/api/stop': app.stop()
                elif route=='/api/select': app.select(data['id'])
                elif route=='/api/decision': app.decision(data['id'],data['decision'])
                elif route=='/api/trim': app.trim(data['id'],data['edge'],data['delta'])
                elif route=='/api/chronology': app.chronology(data['times'])
                elif route=='/api/export': app.start_export()
                elif route=='/api/cleanup/prepare': result=app.prepare_cleanup()
                elif route=='/api/cleanup/confirm': result=app.cleanup(data['ticket'])
                elif route=='/api/shutdown':
                    app.close()
                    self.send(200,{'ok':True})
                    threading.Thread(target=self.server.shutdown,daemon=True).start()
                    return
                elif route=='/api/open-export':
                    state=app.state()
                    if not state['export']: raise ValueError('Najpierw wygeneruj eksport.')
                    os.startfile(state['export']['folder'])
                else: self.send(404,{}); return
                self.send(200,result)
            except (ValueError,OSError,KeyError,StopIteration) as exc:
                self.send(400,{'error':str(exc)})
    return ThreadingHTTPServer(('127.0.0.1',port),Handler)


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--data',default=str(Path(__file__).with_name('data')))
    parser.add_argument('--port',type=int,default=8770)
    parser.add_argument('--open',action='store_true')
    args=parser.parse_args()
    root=Path(args.data)
    reject_links(root)
    root.mkdir(parents=True,exist_ok=True)
    # A second instance must not mark the first instance's analysis interrupted.
    lockfile=(root/'instance.lock').open('a+b')
    if os.name=='nt':
        import msvcrt
        lockfile.seek(0); lockfile.write(b'1'); lockfile.flush(); lockfile.seek(0)
        try: msvcrt.locking(lockfile.fileno(),msvcrt.LK_NBLCK,1)
        except OSError:
            webbrowser.open(f'http://127.0.0.1:{args.port}')
            print('Aplikacja już działa.'); return
    app=Application(root)
    server=make_server(app,args.port)
    url=f'http://127.0.0.1:{server.server_port}'
    print('LPX AI video review: '+url+'\nPozostaw to okno otwarte podczas pracy.',flush=True)
    if args.open: webbrowser.open(url)
    try: server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        app.close()
        server.server_close()
        lockfile.close()


if __name__=='__main__':
    main()
