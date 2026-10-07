"""Clipper HTTP service for n8n (same auth/token as the LLM bridge).

POST /v1/prepare {source_url}                         -> {job_id}   download + transcribe
POST /v1/render  {video_id, clip_id, parts: [{start_id, end_id, role}], context_text?,
                  cover_band?: auto|off|[y0,y1], source_label?}  -> {job_id}   render 1080x1920 short
GET  /v1/jobs/<job_id>                                -> {status: queued|running|done|error, result, error_code, error_message}
GET  /healthz

Jobs run one at a time (GPU/Neural Engine bound) and are persisted under WORK_DIR/jobs.
"""

from __future__ import annotations

import hmac
import json
import os
import queue
import re
import threading
import time
import traceback
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def load_dotenv(path: Path) -> dict:
    env = {}
    if path.exists():
        for line in path.read_text().splitlines():
            m = re.match(r'^([A-Z0-9_]+)=(.*)$', line)
            if m:
                env[m.group(1)] = m.group(2).strip()
    return env


DOTENV = load_dotenv(ROOT / '.env')


def cfg(key: str, default: str | None = None) -> str | None:
    return os.environ.get(key) or DOTENV.get(key) or default


TOKEN = cfg('LLM_BRIDGE_TOKEN')
HOST = cfg('CLIPPER_HOST', '127.0.0.1')
PORT = int(cfg('CLIPPER_PORT', '8788'))
WORK_DIR = Path(cfg('CLIPPER_WORK_DIR', '/Volumes/MacNVMe/pavanatto-cuts/work'))
DOWNLOADS_DIR = Path(cfg('DOWNLOADS_DIR', str(ROOT / 'downloads')))  # mounted as /files in n8n
os.environ.setdefault('CLIPPER_CACHE', cfg('CLIPPER_CACHE', '/Volumes/MacNVMe/pavanatto-cuts/clipper-cache'))
os.environ.setdefault('HF_HOME', os.path.join(os.environ['CLIPPER_CACHE'], 'hf'))

import pipeline  # noqa: E402  (after env setup)

JOBS_DIR = WORK_DIR / 'jobs'
JOBS_DIR.mkdir(parents=True, exist_ok=True)
jobs_q: queue.Queue[str] = queue.Queue()
lock = threading.Lock()


def log(**kw):
    print(json.dumps({'ts': time.strftime('%Y-%m-%dT%H:%M:%S'), **kw}, ensure_ascii=False), flush=True)


def job_path(job_id: str) -> Path:
    return JOBS_DIR / f'{job_id}.json'


def save_job(job: dict) -> None:
    with lock:
        tmp = job_path(job['id']).with_suffix('.tmp')
        tmp.write_text(json.dumps(job, ensure_ascii=False))
        tmp.replace(job_path(job['id']))


def load_job(job_id: str) -> dict | None:
    if not re.fullmatch(r'[a-f0-9]{32}', job_id):
        return None
    p = job_path(job_id)
    return json.loads(p.read_text()) if p.exists() else None


# --- job handlers -------------------------------------------------------------

def do_prepare(params: dict) -> dict:
    url = params['source_url']
    vid = pipeline.youtube_id(url)
    workdir = WORK_DIR / vid
    info = pipeline.download_audio(url, workdir)
    video = pipeline.Background(pipeline.download_video, url, workdir)  # downloads while we transcribe
    video.start()
    tr = pipeline.transcribe(workdir, prompt=f"{info.get('title') or ''}. Lucas Pavanato.")
    video.join()
    if video.error:
        raise video.error
    band = pipeline.detect_overlay_band(workdir)
    return {
        'video_id': vid,
        'source_url': url,
        **info,
        'overlay_band': band,
        'sentences': [{k: s[k] for k in ('id', 'start', 'end', 'text')} for s in tr['sentences']],
        'transcript_model': tr.get('model'),
    }


def do_render(params: dict) -> dict:
    vid = params['video_id']
    if not re.fullmatch(r'[A-Za-z0-9_-]{11}', vid):
        raise pipeline.PipelineError('INVALID_PARAMS', 'video_id')
    workdir = WORK_DIR / vid
    tr = pipeline.transcribe(workdir)
    n = len(tr['sentences'])
    parts = params.get('parts')
    if not isinstance(parts, list) or not parts or not all(
        isinstance(p, dict) and 0 <= int(p.get('start_id', -1)) <= int(p.get('end_id', -1)) < n for p in parts
    ):
        raise pipeline.PipelineError('INVALID_PARAMS', f'parts must be sentence ranges within 0..{n - 1}')

    cover = params.get('cover_band', 'auto')
    if cover == 'auto':
        cover = pipeline.detect_overlay_band(workdir)
    elif cover in (None, 'off', False):
        cover = None
    elif not (isinstance(cover, list) and len(cover) == 2 and 0 <= cover[0] < cover[1] <= 1):
        raise pipeline.PipelineError('INVALID_PARAMS', 'cover_band must be auto|off|[y0,y1]')

    clip_id = re.sub(r'[^A-Za-z0-9_-]', '', str(params.get('clip_id', 'clip')))[:60] or 'clip'
    name = f"{time.strftime('%Y-%m-%d')}_{clip_id}.mp4"
    out = DOWNLOADS_DIR / name
    res = pipeline.render(workdir, tr, parts, out,
                          context_text=(params.get('context_text') or '').strip()[:140] or None,
                          cover_band=cover,
                          source_label=str(params.get('source_label') or '')[:40],
                          sentence_fixes={str(k): str(v)[:1000] for k, v in (params.get('sentence_fixes') or {}).items()
                                          if str(k).isdigit() and int(k) < n})
    return {**res, 'file_name': name, 'container_path': f'/files/{name}'}


HANDLERS = {'prepare': do_prepare, 'render': do_render}


def worker():
    while True:
        job_id = jobs_q.get()
        job = load_job(job_id)
        if not job:
            continue
        job.update(status='running', started_at=time.time())
        save_job(job)
        try:
            job['result'] = HANDLERS[job['kind']](job['params'])
            job['status'] = 'done'
        except pipeline.PipelineError as e:
            job.update(status='error', error_code=e.code, error_message=str(e)[:1000])
        except Exception as e:  # noqa: BLE001
            job.update(status='error', error_code='CLIPPER_FAILED', error_message=f'{e!r}'[:1000])
            traceback.print_exc()
        job['finished_at'] = time.time()
        save_job(job)
        log(event='job', kind=job['kind'], id=job_id, status=job['status'], error_code=job.get('error_code'),
            seconds=round(job['finished_at'] - job['started_at'], 1))


# --- http ---------------------------------------------------------------------

class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):  # silence default access log
        pass

    def send(self, status: int, body: dict):
        data = json.dumps(body, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header('content-type', 'application/json; charset=utf-8')
        self.send_header('content-length', str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def authorized(self) -> bool:
        given = (self.headers.get('authorization') or '').removeprefix('Bearer ').strip()
        return bool(TOKEN) and hmac.compare_digest(given, TOKEN)

    def do_GET(self):
        if self.path == '/healthz':
            return self.send(200, {'status': 'ok', 'queued': jobs_q.qsize()})
        if not self.authorized():
            return self.send(401, {'error_code': 'UNAUTHORIZED'})
        m = re.fullmatch(r'/v1/jobs/([a-f0-9]{32})', self.path)
        job = load_job(m.group(1)) if m else None
        if not job:
            return self.send(404, {'error_code': 'NOT_FOUND'})
        return self.send(200, job)

    def do_POST(self):
        if not self.authorized():
            return self.send(401, {'error_code': 'UNAUTHORIZED'})
        kind = {'/v1/prepare': 'prepare', '/v1/render': 'render'}.get(self.path)
        if not kind:
            return self.send(404, {'error_code': 'NOT_FOUND'})
        try:
            length = int(self.headers.get('content-length') or 0)
            if length > 1_000_000:
                raise ValueError('body too large')
            params = json.loads(self.rfile.read(length) or b'{}')
            required = ['source_url'] if kind == 'prepare' else ['video_id', 'parts']
            missing = [k for k in required if params.get(k) in (None, '')]
            if missing:
                raise ValueError(f'missing {missing}')
        except (ValueError, json.JSONDecodeError) as e:
            return self.send(400, {'error_code': 'INVALID_BODY', 'error_message': str(e)})
        job = {'id': uuid.uuid4().hex, 'kind': kind, 'params': params, 'status': 'queued', 'created_at': time.time()}
        save_job(job)
        jobs_q.put(job['id'])
        return self.send(202, {'job_id': job['id'], 'status': 'queued'})


def main():
    if not TOKEN or len(TOKEN) < 32:
        raise SystemExit('LLM_BRIDGE_TOKEN missing in .env')
    # Jobs interrupted by a restart are marked as errors so n8n stops polling them.
    for p in JOBS_DIR.glob('*.json'):
        job = json.loads(p.read_text())
        if job.get('status') in ('queued', 'running'):
            job.update(status='error', error_code='CLIPPER_RESTARTED', error_message='service restarted')
            save_job(job)
    threading.Thread(target=worker, daemon=True).start()
    log(event='listening', host=HOST, port=PORT)
    ThreadingHTTPServer((HOST, PORT), Handler).serve_forever()


if __name__ == '__main__':
    main()
