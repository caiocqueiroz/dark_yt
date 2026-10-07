#!/usr/bin/env python3
"""Read-only file browser for the rendered videos (HTTP Range support, so videos play/seek in Safari).

Binds to FILES_HOST (default: the Tailscale IP) and requires basic auth (user "radar",
password FILES_PASSWORD from .env). Serves DOWNLOADS_DIR only; no uploads, no deletes.
"""

import base64
import hmac
import html
import mimetypes
import os
import re
import urllib.parse
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parent.parent


def load_dotenv() -> dict:
    env = {}
    for line in (ROOT_DIR / '.env').read_text().splitlines():
        m = re.match(r'^([A-Z0-9_]+)=(.*)$', line)
        if m:
            env[m.group(1)] = m.group(2).strip()
    return env


ENV = load_dotenv()
SERVE = Path(ENV.get('DOWNLOADS_DIR') or ROOT_DIR / 'downloads').resolve()
HOST = os.environ.get('FILES_HOST') or ENV.get('FILES_HOST') or ENV.get('TAILSCALE_IP') or '127.0.0.1'
PORT = int(ENV.get('FILES_PORT', '8090'))
PASSWORD = ENV.get('FILES_PASSWORD', '')
EXPECTED = 'Basic ' + base64.b64encode(f'radar:{PASSWORD}'.encode()).decode()
CHUNK = 1024 * 1024


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def deny(self):
        self.send_response(401)
        self.send_header('WWW-Authenticate', 'Basic realm="Radar Patriota"')
        self.end_headers()

    def resolve(self) -> Path | None:
        rel = urllib.parse.unquote(urllib.parse.urlparse(self.path).path).lstrip('/')
        p = (SERVE / rel).resolve()
        return p if p == SERVE or SERVE in p.parents else None

    def do_HEAD(self):
        self.do_GET(head=True)

    def do_GET(self, head: bool = False):
        if not PASSWORD or not hmac.compare_digest(self.headers.get('Authorization', ''), EXPECTED):
            return self.deny()
        p = self.resolve()
        if p is None or not p.exists() or p.name.startswith('.') or p.suffix == '.part' or '.tmp.' in p.name:
            self.send_error(404)
            return
        if p.is_dir():
            return self.listing(p)
        size = p.stat().st_size
        start, end = 0, size - 1
        rng = re.match(r'bytes=(\d*)-(\d*)', self.headers.get('Range', ''))
        if rng and (rng.group(1) or rng.group(2)):
            if rng.group(1):
                start = int(rng.group(1))
                end = int(rng.group(2)) if rng.group(2) else size - 1
            else:
                start = max(0, size - int(rng.group(2)))
            end = min(end, size - 1)
            self.send_response(206)
            self.send_header('Content-Range', f'bytes {start}-{end}/{size}')
        else:
            self.send_response(200)
        self.send_header('Content-Type', mimetypes.guess_type(p.name)[0] or 'application/octet-stream')
        self.send_header('Content-Length', str(end - start + 1))
        self.send_header('Accept-Ranges', 'bytes')
        self.end_headers()
        if head:
            return
        with p.open('rb') as fh:
            fh.seek(start)
            left = end - start + 1
            while left > 0:
                buf = fh.read(min(CHUNK, left))
                if not buf:
                    break
                try:
                    self.wfile.write(buf)
                except (BrokenPipeError, ConnectionResetError):
                    return
                left -= len(buf)

    def listing(self, folder: Path):
        items = sorted((x for x in folder.iterdir() if not x.name.startswith('.') and '.tmp.' not in x.name),
                       key=lambda x: x.stat().st_mtime, reverse=True)
        rows = []
        for x in items:
            rel = urllib.parse.quote(str(x.relative_to(SERVE)) + ('/' if x.is_dir() else ''))
            when = datetime.fromtimestamp(x.stat().st_mtime).strftime('%d/%m %H:%M')
            size = '' if x.is_dir() else f'{x.stat().st_size / 1e6:.1f} MB'
            thumb = ''
            if x.suffix == '.mp4' and x.with_suffix('.jpg').exists():
                thumb = f'<img src="/{urllib.parse.quote(str(x.with_suffix(".jpg").relative_to(SERVE)))}">'
            rows.append(f'<tr><td>{thumb}</td><td><a href="/{rel}">{html.escape(x.name)}</a></td>'
                        f'<td>{size}</td><td>{when}</td></tr>')
        body = f'''<!doctype html><meta charset="utf-8"><meta name="viewport" content="width=device-width">
<title>Radar Patriota — arquivos</title>
<style>body{{font-family:-apple-system,sans-serif;margin:16px;background:#111;color:#eee}}
a{{color:#ffd600}}td{{padding:6px 10px;border-bottom:1px solid #333;vertical-align:middle}}
img{{height:54px;border-radius:4px}}table{{border-collapse:collapse;width:100%}}</style>
<h2>{html.escape(str(folder.relative_to(SERVE)) if folder != SERVE else 'Radar Patriota — vídeos')}</h2>
<table>{''.join(rows) or '<tr><td>vazio</td></tr>'}</table>'''.encode()
        self.send_response(200)
        self.send_header('Content-Type', 'text/html; charset=utf-8')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)


if __name__ == '__main__':
    if not PASSWORD:
        raise SystemExit('FILES_PASSWORD missing in .env')
    print(f'serving {SERVE} on http://{HOST}:{PORT}', flush=True)
    ThreadingHTTPServer((HOST, PORT), Handler).serve_forever()
