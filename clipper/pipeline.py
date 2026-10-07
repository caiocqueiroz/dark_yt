"""Local clipping pipeline: download -> transcribe -> render vertical short.

Rendering is done frame by frame in Python (crop/reframe + captions + branding)
and piped to ffmpeg's VideoToolbox encoder, so no libass/drawtext is needed.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

import numpy as np

FFMPEG = os.environ.get('FFMPEG_BIN', '/opt/homebrew/bin/ffmpeg')
FFPROBE = os.environ.get('FFPROBE_BIN', '/opt/homebrew/bin/ffprobe')
YTDLP = os.environ.get('YTDLP_BIN', '/opt/homebrew/bin/yt-dlp')
CACHE = Path(os.environ.get('CLIPPER_CACHE', '/Volumes/MacNVMe/pavanatto-cuts/clipper-cache'))
WHISPER_MODEL = os.environ.get('WHISPER_MODEL', 'mlx-community/whisper-large-v3-turbo')
FONT_BOLD = os.environ.get('CLIPPER_FONT', '/System/Library/Fonts/Supplemental/Arial Black.ttf')

OUT_W, OUT_H = 1080, 1920
ANALYSIS_FPS = 5


class PipelineError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(f'{code}: {message}')
        self.code = code


def run(cmd: list[str], code: str, timeout: int = 3600) -> subprocess.CompletedProcess:
    p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    if p.returncode != 0:
        raise PipelineError(code, (p.stderr or p.stdout)[-800:])
    return p


def youtube_id(url: str) -> str:
    m = re.search(r'(?:v=|youtu\.be/|shorts/|live/)([A-Za-z0-9_-]{11})', url)
    if not m:
        raise PipelineError('INVALID_SOURCE_URL', url)
    return m.group(1)


# --- download ----------------------------------------------------------------

def download(url: str, workdir: Path) -> dict:
    workdir.mkdir(parents=True, exist_ok=True)
    src = workdir / 'source.mp4'
    info_path = workdir / 'source.info.json'
    if not src.exists():
        run([
            YTDLP, '--no-playlist', '--no-progress',
            # Prefer plain-https H.264 (hardware decode, sane size) over premium HLS/VP9/AV1.
            '-f', 'bv*[height<=1080][vcodec^=avc1][protocol^=https]+ba[ext=m4a]/bv*[height<=1080]+ba/b[height<=1080]',
            '-S', 'res:1080,vcodec:avc1,proto:https',
            '--merge-output-format', 'mp4', '--write-info-json',
            '-o', str(workdir / 'source.%(ext)s'), url,
        ], 'VIDEO_DOWNLOAD_FAILED')
    if not src.exists():
        raise PipelineError('VIDEO_DOWNLOAD_FAILED', 'source.mp4 not created')
    info = json.loads(info_path.read_text()) if info_path.exists() else {}
    return {
        'title': info.get('title'),
        'channel': info.get('channel') or info.get('uploader'),
        'channel_id': info.get('channel_id'),
        'upload_date': info.get('upload_date'),
        'duration': info.get('duration'),
        'description': (info.get('description') or '')[:2000],
    }


def probe(path: Path) -> dict:
    p = run([FFPROBE, '-v', 'error', '-select_streams', 'v:0', '-show_entries',
             'stream=width,height,r_frame_rate', '-of', 'json', str(path)], 'PROBE_FAILED')
    s = json.loads(p.stdout)['streams'][0]
    num, den = s['r_frame_rate'].split('/')
    return {'width': int(s['width']), 'height': int(s['height']), 'fps': float(num) / float(den)}


# --- transcription -----------------------------------------------------------

def transcribe(workdir: Path, prompt: str | None = None) -> dict:
    out = workdir / 'transcript.json'
    if out.exists():
        return json.loads(out.read_text())
    import mlx_whisper

    os.environ.setdefault('HF_HOME', str(CACHE / 'hf'))
    res = mlx_whisper.transcribe(
        str(workdir / 'source.mp4'),
        path_or_hf_repo=WHISPER_MODEL,
        language='pt',
        word_timestamps=True,
        condition_on_previous_text=False,
        initial_prompt=prompt,
    )
    segments = []
    for i, s in enumerate(res.get('segments', [])):
        words = [
            {'w': w['word'].strip(), 'start': round(w['start'], 2), 'end': round(w['end'], 2)}
            for w in s.get('words', []) if w.get('word', '').strip()
        ]
        if not words:
            continue
        segments.append({
            'id': len(segments),
            'start': words[0]['start'],
            'end': words[-1]['end'],
            'text': s['text'].strip(),
            'words': words,
        })
    data = {'language': res.get('language', 'pt'), 'model': WHISPER_MODEL, 'segments': segments}
    out.write_text(json.dumps(data, ensure_ascii=False))
    return data


# --- frame io ----------------------------------------------------------------

def read_frames(src: Path, start: float, dur: float, w: int, h: int, fps: float | None = None):
    vf = [f'scale={w}:{h}']
    if fps:
        vf.insert(0, f'fps={fps}')
    cmd = [FFMPEG, '-v', 'error', '-ss', f'{start:.3f}', '-t', f'{dur:.3f}', '-i', str(src),
           '-vf', ','.join(vf), '-f', 'rawvideo', '-pix_fmt', 'bgr24', '-']
    p = subprocess.Popen(cmd, stdout=subprocess.PIPE)
    size = w * h * 3
    try:
        while True:
            buf = p.stdout.read(size)
            if len(buf) < size:
                break
            yield np.frombuffer(buf, np.uint8).reshape(h, w, 3)
    finally:
        p.stdout.close()
        p.wait()


# --- analysis: shots + faces + active speaker -------------------------------

@dataclass
class Face:
    cx: float  # normalized 0..1
    cy: float
    size: float  # normalized face height
    jaw: float


@lru_cache(maxsize=1)
def face_landmarker():
    from mediapipe.tasks.python import BaseOptions, vision

    opts = vision.FaceLandmarkerOptions(
        base_options=BaseOptions(model_asset_path=str(CACHE / 'face_landmarker.task')),
        num_faces=4,
        min_face_detection_confidence=0.5,
        output_face_blendshapes=True,
    )
    return vision.FaceLandmarker.create_from_options(opts)


def detect_faces(frame_bgr: np.ndarray) -> list[Face]:
    import mediapipe as mp

    img = mp.Image(image_format=mp.ImageFormat.SRGB, data=np.ascontiguousarray(frame_bgr[:, :, ::-1]))
    res = face_landmarker().detect(img)
    faces = []
    for lm, bs in zip(res.face_landmarks, res.face_blendshapes or [[]] * len(res.face_landmarks)):
        xs = [p.x for p in lm]
        ys = [p.y for p in lm]
        jaw = next((b.score for b in bs if b.category_name == 'jawOpen'), 0.0)
        faces.append(Face(cx=(min(xs) + max(xs)) / 2, cy=(min(ys) + max(ys)) / 2, size=max(ys) - min(ys), jaw=jaw))
    return faces


def analyze(src: Path, start: float, dur: float, width: int, height: int) -> list[dict]:
    """Returns one entry per analysis frame: {t, shot, faces}."""
    aw = 640
    ah = int(round(height * aw / width / 2) * 2)
    frames = []
    prev_hist = None
    shot = 0
    for i, f in enumerate(read_frames(src, start, dur, aw, ah, fps=ANALYSIS_FPS)):
        import cv2

        hsv = cv2.cvtColor(f, cv2.COLOR_BGR2HSV)
        hist = cv2.calcHist([hsv], [0, 1], None, [32, 32], [0, 180, 0, 256])
        cv2.normalize(hist, hist)
        if prev_hist is not None and cv2.compareHist(prev_hist, hist, cv2.HISTCMP_CORREL) < 0.6:
            shot += 1
        prev_hist = hist
        frames.append({'t': i / ANALYSIS_FPS, 'shot': shot, 'faces': detect_faces(f)})
    return frames


def plan_crops(frames: list[dict], src_w: int, src_h: int, window: float = 1.0, min_hold: float = 1.5) -> list[dict]:
    """Decides, per analysis window, which horizontal crop center to use (or None = fit layout).

    Within a shot, faces are grouped into tracks by x position; the active speaker is the
    track whose mouth (jawOpen) varies the most in that window.
    """
    if not frames:
        return []
    crop_w_norm = (src_h * 9 / 16) / src_w
    decisions = []
    shots = sorted({f['shot'] for f in frames})
    for s in shots:
        sf = [f for f in frames if f['shot'] == s]
        # Build tracks by clustering face centers along x.
        tracks: list[list[float]] = []
        for f in sf:
            for face in f['faces']:
                for tr in tracks:
                    if abs(np.median(tr) - face.cx) < 0.08:
                        tr.append(face.cx)
                        break
                else:
                    tracks.append([face.cx])
        centers = [float(np.median(t)) for t in tracks if len(t) >= max(2, len(sf) * 0.2)]

        t0, t1 = sf[0]['t'], sf[-1]['t'] + 1 / ANALYSIS_FPS
        t = t0
        while t < t1:
            win = [f for f in sf if t <= f['t'] < t + window]
            choice = None
            if centers:
                # Faces all fit in one crop -> center on the group.
                if max(centers) - min(centers) < crop_w_norm * 0.8:
                    choice = (max(centers) + min(centers)) / 2
                else:
                    scores = []
                    for c in centers:
                        jaws = [face.jaw for f in win for face in f['faces'] if abs(face.cx - c) < 0.08]
                        sizes = [face.size for f in win for face in f['faces'] if abs(face.cx - c) < 0.08]
                        activity = float(np.std(jaws)) + 0.5 * float(np.mean(jaws)) if jaws else -1
                        scores.append((activity, float(np.mean(sizes)) if sizes else 0, c))
                    best = max(scores)
                    choice = best[2] if best[0] >= 0 else None
            decisions.append({'t0': t, 't1': min(t + window, t1), 'shot': s, 'cx': choice})
            t += window

    # Hysteresis: don't switch speaker inside a shot unless the new choice holds >= min_hold.
    smoothed = []
    for d in decisions:
        if smoothed and smoothed[-1]['shot'] == d['shot'] and d['cx'] is not None and smoothed[-1]['cx'] is not None:
            prev = smoothed[-1]
            if abs(prev['cx'] - d['cx']) > 0.05:
                ahead = [x for x in decisions if x['shot'] == d['shot'] and d['t0'] <= x['t0'] < d['t0'] + min_hold]
                if not all(x['cx'] is not None and abs(x['cx'] - d['cx']) < 0.05 for x in ahead):
                    d = {**d, 'cx': prev['cx']}
        smoothed.append(d)
    # Merge adjacent equal decisions.
    merged = []
    for d in smoothed:
        if merged and merged[-1]['shot'] == d['shot'] and (
            (merged[-1]['cx'] is None and d['cx'] is None)
            or (merged[-1]['cx'] is not None and d['cx'] is not None and abs(merged[-1]['cx'] - d['cx']) < 0.02)
        ):
            merged[-1]['t1'] = d['t1']
        else:
            merged.append(dict(d))
    return merged


# --- captions & branding -----------------------------------------------------

@lru_cache(maxsize=8)
def font(size: int):
    from PIL import ImageFont

    return ImageFont.truetype(FONT_BOLD, size)


def caption_chunks(words: list[dict], max_words: int = 4, max_chars: int = 26) -> list[list[dict]]:
    chunks, cur = [], []
    for w in words:
        text = ' '.join(x['w'] for x in cur + [w])
        gap = w['start'] - cur[-1]['end'] if cur else 0
        if cur and (len(cur) >= max_words or len(text) > max_chars or gap > 0.6):
            chunks.append(cur)
            cur = []
        cur.append(w)
        if re.search(r'[.!?]$', w['w']):
            chunks.append(cur)
            cur = []
    if cur:
        chunks.append(cur)
    return chunks


def wrap(words: list[str], f, max_w: int) -> list[list[int]] | None:
    """Greedy word wrap; returns None if it needs more than 2 lines or a word is too wide."""
    lines, cur = [], []
    for i, w in enumerate(words):
        if f.getlength(w) > max_w:
            return None
        test = ' '.join(words[j] for j in cur + [i])
        if cur and f.getlength(test) > max_w:
            lines.append(cur)
            cur = []
        cur.append(i)
    if cur:
        lines.append(cur)
    return lines if len(lines) <= 2 else None


@lru_cache(maxsize=4096)
def caption_image(words: tuple[str, ...], active: int):
    """RGBA image of a caption chunk with the active word highlighted."""
    from PIL import Image, ImageDraw

    words_up = [w.upper() for w in words]
    max_w = OUT_W - 140
    for size in (76, 70, 64, 58, 52, 46):  # shrink until it fits in 2 lines
        f = font(size)
        lines = wrap(words_up, f, max_w)
        if lines:
            break
    else:
        lines = [list(range(len(words_up)))]
    line_h = int(size * 1.3)
    img = Image.new('RGBA', (OUT_W, line_h * len(lines) + 40), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    space = f.getlength(' ')
    for li, idxs in enumerate(lines):
        total = sum(f.getlength(words_up[i]) for i in idxs) + space * (len(idxs) - 1)
        x = (OUT_W - total) / 2
        y = 20 + li * line_h
        for i in idxs:
            color = (255, 214, 0, 255) if i == active else (255, 255, 255, 255)
            d.text((x, y), words_up[i], font=f, fill=color, stroke_width=7, stroke_fill=(0, 0, 0, 255))
            x += f.getlength(words_up[i]) + space
    return np.array(img)


@lru_cache(maxsize=1)
def branding_image():
    from PIL import Image, ImageDraw

    img = Image.new('RGBA', (OUT_W, 200), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    for text, size, y, alpha in (('PAVANATTO AGORA', 44, 40, 235), ('CANAL FÃ • NÃO OFICIAL', 26, 98, 200)):
        f = font(size)
        x = (OUT_W - f.getlength(text)) / 2
        d.text((x, y), text, font=f, fill=(255, 255, 255, alpha), stroke_width=4, stroke_fill=(0, 0, 0, 160))
    return np.array(img)


def blend(frame: np.ndarray, rgba: np.ndarray, y: int) -> None:
    h = min(rgba.shape[0], frame.shape[0] - y)
    if h <= 0:
        return
    region = frame[y:y + h, :rgba.shape[1]]
    alpha = rgba[:h, :, 3:4].astype(np.float32) / 255
    rgb = rgba[:h, :, 2::-1].astype(np.float32)  # RGBA -> BGR
    region[:] = (rgb * alpha + region.astype(np.float32) * (1 - alpha)).astype(np.uint8)


# --- render -----------------------------------------------------------------

def compose(frame: np.ndarray, cx: float | None, src_w: int, src_h: int) -> np.ndarray:
    import cv2

    if cx is not None:
        cw = int(round(src_h * 9 / 16))
        x = int(round(cx * src_w - cw / 2))
        x = max(0, min(src_w - cw, x))
        return cv2.resize(frame[:, x:x + cw], (OUT_W, OUT_H), interpolation=cv2.INTER_CUBIC)
    # Fit layout: full frame centered over a blurred, zoomed background.
    bg = cv2.resize(frame, (int(OUT_H * src_w / src_h), OUT_H))
    off = (bg.shape[1] - OUT_W) // 2
    bg = cv2.GaussianBlur(bg[:, off:off + OUT_W], (0, 0), 30)
    bg = (bg * 0.6).astype(np.uint8)
    fh = int(OUT_W * src_h / src_w)
    fg = cv2.resize(frame, (OUT_W, fh), interpolation=cv2.INTER_AREA)
    y = (OUT_H - fh) // 2
    bg[y:y + fh] = fg
    return bg


def render(workdir: Path, start: float, end: float, out_path: Path, words: list[dict]) -> dict:
    src = workdir / 'source.mp4'
    meta = probe(src)
    sw, sh, fps = meta['width'], meta['height'], meta['fps']
    dur = end - start

    frames = analyze(src, start, dur, sw, sh)
    crops = plan_crops(frames, sw, sh)

    rel_words = [
        {'w': w['w'], 'start': w['start'] - start, 'end': w['end'] - start}
        for w in words if w['end'] > start and w['start'] < end
    ]
    chunks = caption_chunks(rel_words)
    timeline = []  # (t0, t1, words tuple, active index)
    for ci, ch in enumerate(chunks):
        texts = tuple(w['w'] for w in ch)
        nxt = chunks[ci + 1][0]['start'] if ci + 1 < len(chunks) else dur
        for wi, w in enumerate(ch):
            t1 = ch[wi + 1]['start'] if wi + 1 < len(ch) else min(nxt, w['end'] + 0.4)
            timeline.append((w['start'], t1, texts, wi))

    out_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = out_path.with_suffix('.tmp.mp4')
    enc = subprocess.Popen([
        FFMPEG, '-v', 'error', '-y',
        '-f', 'rawvideo', '-pix_fmt', 'bgr24', '-s', f'{OUT_W}x{OUT_H}', '-r', f'{fps}', '-i', '-',
        '-ss', f'{start:.3f}', '-t', f'{dur:.3f}', '-i', str(src),
        '-map', '0:v', '-map', '1:a',
        '-c:v', 'h264_videotoolbox', '-b:v', '12M', '-profile:v', 'high', '-pix_fmt', 'yuv420p',
        '-c:a', 'aac', '-b:a', '192k', '-af', 'loudnorm=I=-14:TP=-1.5:LRA=11',
        '-movflags', '+faststart', '-shortest', str(tmp),
    ], stdin=subprocess.PIPE, stderr=subprocess.PIPE)

    brand = branding_image()
    ci = 0
    n = 0
    fit_frames = 0
    try:
        for n, frame in enumerate(read_frames(src, start, dur, sw, sh)):
            t = n / fps
            d = next((c for c in crops if c['t0'] <= t < c['t1']), crops[-1] if crops else {'cx': None})
            if d['cx'] is None:
                fit_frames += 1
            out = compose(frame, d['cx'], sw, sh)
            blend(out, brand, 60)
            while ci < len(timeline) and timeline[ci][1] <= t:
                ci += 1
            if ci < len(timeline) and timeline[ci][0] <= t:
                _, _, texts, active = timeline[ci]
                blend(out, caption_image(texts, active), int(OUT_H * 0.66))
            enc.stdin.write(out.tobytes())
    finally:
        enc.stdin.close()
        err = enc.stderr.read().decode()
        enc.wait()
    if enc.returncode != 0:
        raise PipelineError('RENDER_FAILED', err[-800:])
    tmp.replace(out_path)

    out_meta = probe(out_path)
    return {
        'path': str(out_path),
        'width': out_meta['width'],
        'height': out_meta['height'],
        'duration': round(dur, 2),
        'frames': n + 1,
        'shots': len({f['shot'] for f in frames}),
        'fit_ratio': round(fit_frames / max(1, n + 1), 3),
        'crop_plan': [{**c, 't0': round(c['t0'], 2), 't1': round(c['t1'], 2),
                       'cx': None if c['cx'] is None else round(c['cx'], 3)} for c in crops],
    }
