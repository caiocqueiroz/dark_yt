"""Local clipping pipeline: download -> transcribe -> render vertical short.

Rendering is done frame by frame in Python (reframe, captions, branding, context and
broadcaster-band cover) and piped to ffmpeg/libx264, so no libass/drawtext is needed.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import threading
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

import numpy as np

FFMPEG = os.environ.get('FFMPEG_BIN', '/opt/homebrew/bin/ffmpeg')
FFPROBE = os.environ.get('FFPROBE_BIN', '/opt/homebrew/bin/ffprobe')
YTDLP = os.environ.get('YTDLP_BIN', '/opt/homebrew/bin/yt-dlp')
CACHE = Path(os.environ.get('CLIPPER_CACHE', '/Volumes/MacNVMe/pavanatto-cuts/clipper-cache'))
WHISPER_MODEL = os.environ.get('WHISPER_MODEL', 'mlx-community/whisper-large-v3-turbo')
FONT_BOLD = os.environ.get('CLIPPER_FONT') or (
    str(CACHE / 'fonts' / 'Montserrat-Black.ttf') if (CACHE / 'fonts' / 'Montserrat-Black.ttf').exists()
    else '/System/Library/Fonts/Supplemental/Arial Black.ttf')
FONT_TEXT = os.environ.get('CLIPPER_FONT_TEXT', '/System/Library/Fonts/Supplemental/Arial Bold.ttf')

OUT_W, OUT_H = 1080, 1920
BRAND_NAME = os.environ.get('CLIPPER_BRAND', 'Radar Patriota')
BRAND_NOTICE = 'CANAL INDEPENDENTE'
FONTS = CACHE / 'fonts'
MONT_BLACK = str(FONTS / 'Montserrat-Black.ttf')
MONT_XB = str(FONTS / 'Montserrat-ExtraBold.ttf')
MONT_XB_ITALIC = str(FONTS / 'Montserrat-ExtraBoldItalic.ttf')

# "alerta" template layout (1080x1920): image on top, headline bars, clip below.
ALERTA_IMG_H = 830
ALERTA_KICKER = (660, 170)  # y, height (overlaps the bottom of the image)
ALERTA_SUB = (830, 96)
ALERTA_VIDEO_Y = 926
ALERTA_CAPTION_Y = 1290

THEMES = {
    # Brazil palette: green bar / white text, yellow bar / navy text.
    'brasil': {'kicker_bg': (0, 146, 63), 'kicker_fg': (255, 255, 255), 'sub_bg': (255, 214, 0),
               'sub_fg': (0, 39, 118), 'chip_bg': (0, 39, 118), 'chip_fg': (255, 214, 0)},
    # Red alert variant (URGENTE/BOMBA style).
    'alerta': {'kicker_bg': (214, 24, 32), 'kicker_fg': (255, 255, 255), 'sub_bg': (255, 214, 0),
               'sub_fg': (10, 10, 10), 'chip_bg': (0, 146, 63), 'chip_fg': (255, 255, 255)},
}
ANALYSIS_FPS = 5
CAPTION_Y = int(OUT_H * 0.64)
NAME_TAG_EXTENSION = 0.055
YELLOW = (255, 214, 0, 255)


class PipelineError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(f'{code}: {message}')
        self.code = code


def run(cmd: list[str], code: str, timeout: int = 7200) -> subprocess.CompletedProcess:
    p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    if p.returncode != 0:
        raise PipelineError(code, (p.stderr or p.stdout)[-800:])
    return p


def youtube_id(url: str) -> str:
    m = re.search(r'(?:v=|youtu\.be/|shorts/|live/)([A-Za-z0-9_-]{11})', url)
    if not m:
        raise PipelineError('INVALID_SOURCE_URL', url)
    return m.group(1)


def find(workdir: Path, stem: str) -> Path | None:
    hits = [p for p in workdir.glob(f'{stem}.*') if p.suffix not in ('.part', '.ytdl', '.json')]
    return hits[0] if hits else None


# --- download ----------------------------------------------------------------

def download_audio(url: str, workdir: Path) -> dict:
    """Audio + metadata first: enough to transcribe while the video downloads."""
    workdir.mkdir(parents=True, exist_ok=True)
    if not find(workdir, 'audio'):
        run([YTDLP, '--no-playlist', '--no-progress', '-f', 'ba[ext=m4a]/ba', '--write-info-json',
             '-o', str(workdir / 'audio.%(ext)s'), url], 'VIDEO_DOWNLOAD_FAILED')
    if not find(workdir, 'audio'):
        raise PipelineError('VIDEO_DOWNLOAD_FAILED', 'audio not created')
    info_path = workdir / 'audio.info.json'
    info = json.loads(info_path.read_text()) if info_path.exists() else {}
    return {
        'title': info.get('title'),
        'channel': info.get('channel') or info.get('uploader'),
        'channel_id': info.get('channel_id'),
        'upload_date': info.get('upload_date'),
        'duration': info.get('duration'),
        'description': (info.get('description') or '')[:2000],
    }


def download_video(url: str, workdir: Path) -> Path:
    """Video only, highest bitrate up to 1080p (YouTube 'Premium' VP9 when offered)."""
    v = find(workdir, 'video')
    if not v:
        run([YTDLP, '--no-playlist', '--no-progress', '-f', 'bv*[height<=1080]', '-S', 'res:1080,tbr',
             '-o', str(workdir / 'video.%(ext)s'), url], 'VIDEO_DOWNLOAD_FAILED')
        v = find(workdir, 'video')
    if not v:
        raise PipelineError('VIDEO_DOWNLOAD_FAILED', 'video not created')
    return v


class Background(threading.Thread):
    def __init__(self, fn, *args):
        super().__init__(daemon=True)
        self.fn, self.args, self.result, self.error = fn, args, None, None

    def run(self):
        try:
            self.result = self.fn(*self.args)
        except Exception as e:  # noqa: BLE001
            self.error = e


def probe(path: Path) -> dict:
    p = run([FFPROBE, '-v', 'error', '-select_streams', 'v:0', '-show_entries',
             'stream=width,height,r_frame_rate:format=duration', '-of', 'json', str(path)], 'PROBE_FAILED')
    d = json.loads(p.stdout)
    s = d['streams'][0]
    num, den = s['r_frame_rate'].split('/')
    return {'width': int(s['width']), 'height': int(s['height']), 'fps': float(num) / float(den),
            'duration': float(d.get('format', {}).get('duration') or 0)}


# --- transcription -----------------------------------------------------------

SENTENCES_VERSION = 2


def build_sentences(segments: list[dict], max_words: int = 45, max_dur: float = 8.0) -> list[dict]:
    """Regroups word timestamps into sentences, so clips always start/end on full sentences.

    Sentences end at final punctuation or long pauses; any sentence longer than max_dur is split
    at its largest internal pause (often an unpunctuated speaker change).
    """
    words = [w for s in segments for w in s['words']]
    spans, cur0 = [], 0
    for i, w in enumerate(words):
        nxt = words[i + 1] if i + 1 < len(words) else None
        ends = re.search(r'[.!?…]["»”]?$', w['w']) is not None
        long_gap = nxt is not None and nxt['start'] - w['end'] > 1.5
        if ends or long_gap or i - cur0 + 1 >= max_words or nxt is None:
            spans.append((cur0, i))
            cur0 = i + 1

    def split(a: int, b: int):
        if words[b]['end'] - words[a]['start'] <= max_dur or b - a < 4:
            return [(a, b)]
        gaps = [(words[k + 1]['start'] - words[k]['end'], k) for k in range(a + 1, b - 1)]
        gap, k = max(gaps)
        if gap < 0.3:
            return [(a, b)]
        return split(a, k) + split(k + 1, b)

    sentences = []
    for a, b in spans:
        for x, y in split(a, b):
            sentences.append({
                'id': len(sentences),
                'start': words[x]['start'],
                'end': words[y]['end'],
                'text': ' '.join(w['w'] for w in words[x:y + 1]),
                'w0': x,
                'w1': y,
            })
    return sentences


def transcribe(workdir: Path, prompt: str | None = None) -> dict:
    out = workdir / 'transcript.json'
    if out.exists():
        data = json.loads(out.read_text())
    else:
        import mlx_whisper

        res = mlx_whisper.transcribe(
            str(find(workdir, 'audio')),
            path_or_hf_repo=WHISPER_MODEL,
            language='pt',
            word_timestamps=True,
            condition_on_previous_text=False,
            initial_prompt=prompt,
        )
        segments = []
        for s in res.get('segments', []):
            words = [
                {'w': w['word'].strip(), 'start': round(w['start'], 2), 'end': round(w['end'], 2)}
                for w in s.get('words', []) if w.get('word', '').strip()
            ]
            if words:
                segments.append({'id': len(segments), 'start': words[0]['start'], 'end': words[-1]['end'],
                                 'text': s['text'].strip(), 'words': words})
        data = {'language': res.get('language', 'pt'), 'model': WHISPER_MODEL, 'segments': segments}
    if data.get('sentences_version') != SENTENCES_VERSION:
        data['sentences'] = build_sentences(data['segments'])
        data['sentences_version'] = SENTENCES_VERSION
        out.write_text(json.dumps(data, ensure_ascii=False))
    return data


# --- frame io ----------------------------------------------------------------

def read_frames(src: Path, start: float, dur: float, w: int, h: int, fps: float | None = None):
    vf = [f'scale={w}:{h}:flags=lanczos']
    if fps:
        vf.insert(0, f'fps={fps}')
    cmd = [FFMPEG, '-v', 'error', '-ss', f'{start:.3f}', '-t', f'{dur:.3f}', '-i', str(src),
           '-vf', ','.join(vf), '-f', 'rawvideo', '-pix_fmt', 'bgr24', '-']
    # stderr is discarded: closing the pipe early (single-frame grabs) makes ffmpeg log "Broken pipe".
    p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
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


def grab_frame(src: Path, t: float, w: int, h: int) -> np.ndarray | None:
    return next(read_frames(src, t, 0.2, w, h), None)


# --- broadcaster band (lower third) detection --------------------------------

def detect_overlay_band(workdir: Path, samples: int = 48) -> list[float] | None:
    """Finds a static horizontal band at the bottom (e.g. TV lower third / ticker).

    Rows of a burned-in overlay keep the same colors across camera changes, while scene
    rows vary. Returns [y0, y1] as fractions of the frame height, or None.
    """
    cache = workdir / 'overlay.json'
    if cache.exists():
        data = json.loads(cache.read_text())
        if data.get('version') == 3:
            return data.get('band')
    src = find(workdir, 'video')
    meta = probe(src)
    w, h = 320, int(round(meta['height'] * 320 / meta['width'] / 2) * 2)
    dur = meta['duration']
    rows = []
    for i in range(samples):
        f = grab_frame(src, dur * (0.05 + 0.9 * i / (samples - 1)), w, h)
        if f is not None:
            rows.append(f.reshape(h, w, 3).mean(axis=1))  # per-row mean color
    band = None
    if len(rows) >= samples // 2:
        std = np.stack(rows).std(axis=0).mean(axis=1)  # per-row variation across samples
        static = std < 8
        top_static = static[: int(h * 0.55)].mean()
        if top_static < 0.5:  # the scene itself varies -> detection is meaningful
            y = h - 1
            best = None
            while y > h * 0.55:
                if static[y]:
                    y1 = y
                    gap = 0
                    while y > h * 0.55 and (static[y] or gap < h * 0.02):
                        gap = 0 if static[y] else gap + 1
                        y -= 1
                    y0 = y + 1 + gap
                    if (y1 - y0) / h >= 0.03 and (best is None or y1 - y0 > best[1] - best[0]):
                        best = (y0, y1)
                y -= 1
            if best and (best[1] - best[0]) / h <= 0.25:
                # Name tags (lower thirds) appear intermittently right above a fixed ticker and
                # only span part of the width, so they are covered by a fixed extension.
                band = [round(max(0, best[0] / h - 0.006 - NAME_TAG_EXTENSION), 4),
                        round(min(1, (best[1] + 1) / h + 0.006), 4)]
    cache.write_text(json.dumps({'band': band, 'version': 3}))
    return band


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
    """One entry per analysis frame: {t, shot, faces}."""
    import cv2

    aw = 640
    ah = int(round(height * aw / width / 2) * 2)
    frames = []
    prev_hist = None
    shot = 0
    for i, f in enumerate(read_frames(src, start, dur, aw, ah, fps=ANALYSIS_FPS)):
        hsv = cv2.cvtColor(f, cv2.COLOR_BGR2HSV)
        hist = cv2.calcHist([hsv], [0, 1], None, [32, 32], [0, 180, 0, 256])
        cv2.normalize(hist, hist)
        if prev_hist is not None and cv2.compareHist(prev_hist, hist, cv2.HISTCMP_CORREL) < 0.6:
            shot += 1
        prev_hist = hist
        frames.append({'t': i / ANALYSIS_FPS, 'shot': shot, 'faces': detect_faces(f)})
    return frames


def plan_crops(frames: list[dict], src_w: int, src_h: int, window: float = 1.0, min_hold: float = 1.5) -> list[dict]:
    """Per window, the horizontal crop center (or None = fit layout).

    Within a shot, faces are grouped into tracks by x; the active speaker is the track
    whose mouth (jawOpen) moves the most in that window.
    """
    if not frames:
        return []
    crop_w_norm = (src_h * 9 / 16) / src_w
    decisions = []
    for s in sorted({f['shot'] for f in frames}):
        sf = [f for f in frames if f['shot'] == s]
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

    smoothed = []
    for d in decisions:
        if smoothed and smoothed[-1]['shot'] == d['shot'] and d['cx'] is not None and smoothed[-1]['cx'] is not None:
            prev = smoothed[-1]
            if abs(prev['cx'] - d['cx']) > 0.05:
                ahead = [x for x in decisions if x['shot'] == d['shot'] and d['t0'] <= x['t0'] < d['t0'] + min_hold]
                if not all(x['cx'] is not None and abs(x['cx'] - d['cx']) < 0.05 for x in ahead):
                    d = {**d, 'cx': prev['cx']}
        smoothed.append(d)
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


# --- text rendering -------------------------------------------------------------

@lru_cache(maxsize=32)
def font(size: int, path: str = FONT_BOLD):
    from PIL import ImageFont

    return ImageFont.truetype(path, size)


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


def wrap(words: list[str], f, max_w: int, max_lines: int = 2) -> list[list[int]] | None:
    """Greedy word wrap; None if it needs more than max_lines or a word is too wide."""
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
    return lines if len(lines) <= max_lines else None


@lru_cache(maxsize=4096)
def caption_image(words: tuple[str, ...], active: int, highlight: bool = True):
    """RGBA caption chunk with the active word highlighted."""
    from PIL import Image, ImageDraw

    words_up = [w.upper() for w in words]
    max_w = OUT_W - 140
    for size in (76, 70, 64, 58, 52, 46):
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
            color = YELLOW if (highlight and i == active) else (255, 255, 255, 255)
            d.text((x, y), words_up[i], font=f, fill=color, stroke_width=7, stroke_fill=(0, 0, 0, 255))
            x += f.getlength(words_up[i]) + space
    return np.array(img)


@lru_cache(maxsize=8)
def branding_image(brand: str = BRAND_NAME):
    from PIL import Image, ImageDraw

    img = Image.new('RGBA', (OUT_W, 200), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    for text, size, y, alpha in ((brand.upper(), 44, 40, 235), (BRAND_NOTICE, 26, 98, 200)):
        f = font(size)
        x = (OUT_W - f.getlength(text)) / 2
        d.text((x, y), text, font=f, fill=(255, 255, 255, alpha), stroke_width=4, stroke_fill=(0, 0, 0, 160))
    return np.array(img)


@lru_cache(maxsize=8)
def context_card(text: str, label: str = 'CONTEXTO'):
    """Rounded dark card with a yellow label and up to 3 lines of context text."""
    from PIL import Image, ImageDraw

    pad, max_w = 34, OUT_W - 120
    words = text.split()
    for size in (44, 40, 36, 32):
        f = font(size, FONT_TEXT)
        lines = wrap(words, f, max_w - 2 * pad, max_lines=3)
        if lines:
            break
    else:
        lines = [list(range(len(words)))]
    lf = font(28)
    line_h = int(size * 1.25)
    h = pad + 40 + line_h * len(lines) + pad
    img = Image.new('RGBA', (OUT_W, h + 10), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    x0 = (OUT_W - max_w) // 2
    d.rounded_rectangle((x0, 0, x0 + max_w, h), radius=28, fill=(12, 12, 12, 215))
    d.text((x0 + pad, pad - 6), label, font=lf, fill=YELLOW)
    for li, idxs in enumerate(lines):
        d.text((x0 + pad, pad + 40 + li * line_h), ' '.join(words[i] for i in idxs), font=f, fill=(255, 255, 255, 255))
    return np.array(img)


@lru_cache(maxsize=4)
def label_pill(text: str):
    from PIL import Image, ImageDraw

    f = font(34)
    w = int(f.getlength(text)) + 48
    img = Image.new('RGBA', (OUT_W, 64), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    x0 = (OUT_W - w) // 2
    d.rounded_rectangle((x0, 0, x0 + w, 60), radius=30, fill=(255, 214, 0, 240))
    d.text((x0 + 24, 9), text, font=f, fill=(0, 0, 0, 255))
    return np.array(img)


@lru_cache(maxsize=16)
def cover_banner(height: int, source_label: str, brand: str = BRAND_NAME):
    """Channel banner used to cover a broadcaster lower third (also carries the fan-channel notice)."""
    from PIL import Image, ImageDraw

    height = max(24, height)
    img = Image.new('RGBA', (OUT_W, height), (14, 14, 14, 250))
    d = ImageDraw.Draw(img)
    d.rectangle((0, 0, OUT_W, max(3, height // 30)), fill=YELLOW)
    line1 = brand.upper()
    line2 = BRAND_NOTICE + (f'  •  FONTE: {source_label.upper()}' if source_label else '')
    two_lines = height >= 90
    size1 = min(56, int(height * (0.30 if two_lines else 0.42)))
    f1 = font(size1)
    if not two_lines:
        text = f'{line1}  •  {line2}'
        while f1.getlength(text) > OUT_W - 60 and size1 > 12:
            size1 -= 2
            f1 = font(size1)
        bb = f1.getbbox(text)
        d.text(((OUT_W - f1.getlength(text)) / 2, (height - (bb[3] - bb[1])) / 2 - bb[1]), text, font=f1, fill=(255, 255, 255))
        return np.array(img)
    size2 = max(14, int(size1 * 0.5))
    f2 = font(size2)
    while f2.getlength(line2) > OUT_W - 80 and size2 > 12:
        size2 -= 1
        f2 = font(size2)
    b1, b2 = f1.getbbox(line1), f2.getbbox(line2)
    h1, h2, gap = b1[3] - b1[1], b2[3] - b2[1], int(size1 * 0.35)
    y = (height - (h1 + gap + h2)) / 2 + height // 60
    d.text(((OUT_W - f1.getlength(line1)) / 2, y - b1[1]), line1, font=f1, fill=(255, 255, 255))
    d.text(((OUT_W - f2.getlength(line2)) / 2, y + h1 + gap - b2[1]), line2, font=f2, fill=YELLOW)
    return np.array(img)


def blend(frame: np.ndarray, rgba: np.ndarray, y: int, opacity: float = 1.0) -> None:
    y = max(0, y)
    h = min(rgba.shape[0], frame.shape[0] - y)
    if h <= 0:
        return
    region = frame[y:y + h, :rgba.shape[1]]
    alpha = rgba[:h, :, 3:4].astype(np.float32) / 255 * opacity
    rgb = rgba[:h, :, 2::-1].astype(np.float32)  # RGBA -> BGR
    region[:] = (rgb * alpha + region.astype(np.float32) * (1 - alpha)).astype(np.uint8)


# --- compose -----------------------------------------------------------------

def sharpen(img: np.ndarray) -> np.ndarray:
    import cv2

    blur = cv2.GaussianBlur(img, (0, 0), 1.3)
    return cv2.addWeighted(img, 1.4, blur, -0.4, 0)


def compose(frame: np.ndarray, cx: float | None, src_w: int, src_h: int) -> tuple[np.ndarray, tuple[int, int]]:
    """Returns the 1080x1920 frame and (y, height) of the source frame inside it."""
    import cv2

    if cx is not None:
        cw = int(round(src_h * 9 / 16))
        x = int(round(cx * src_w - cw / 2))
        x = max(0, min(src_w - cw, x))
        out = cv2.resize(frame[:, x:x + cw], (OUT_W, OUT_H), interpolation=cv2.INTER_LANCZOS4)
        return sharpen(out), (0, OUT_H)
    bg = cv2.resize(frame, (int(OUT_H * src_w / src_h), OUT_H), interpolation=cv2.INTER_AREA)
    off = (bg.shape[1] - OUT_W) // 2
    bg = cv2.GaussianBlur(bg[:, off:off + OUT_W], (0, 0), 30)
    bg = (bg * 0.6).astype(np.uint8)
    fh = int(OUT_W * src_h / src_w)
    fg = cv2.resize(frame, (OUT_W, fh), interpolation=cv2.INTER_AREA)
    y = (OUT_H - fh) // 2
    bg[y:y + fh] = fg
    return bg, (y, fh)


# --- "alerta" template ----------------------------------------------------------

def fit_font(text: str, path: str, max_w: int, start: int, minimum: int = 20):
    size = start
    f = font(size, path)
    while f.getlength(text) > max_w and size > minimum:
        size -= 2
        f = font(size, path)
    return f


@lru_cache(maxsize=16)
def headline_bars(kicker: str, text: str, theme_name: str):
    """Kicker bar (e.g. 'BOMBA!') + subtitle bar, as one RGBA strip spanning both bars."""
    from PIL import Image, ImageDraw

    th = THEMES.get(theme_name, THEMES['brasil'])
    ky, kh = ALERTA_KICKER
    sy, sh_ = ALERTA_SUB
    img = Image.new('RGBA', (OUT_W, sy + sh_ - ky), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.rectangle((0, 0, OUT_W, kh), fill=(*th['kicker_bg'], 255))
    d.rectangle((0, sy - ky, OUT_W, sy - ky + sh_), fill=(*th['sub_bg'], 255))
    kicker = kicker.upper().strip()
    f = fit_font(kicker, MONT_BLACK, OUT_W - 70, 156, 60)
    bb = f.getbbox(kicker)
    x = (OUT_W - (bb[2] - bb[0])) / 2 - bb[0]
    y = (kh - (bb[3] - bb[1])) / 2 - bb[1]
    d.text((x + 5, y + 6), kicker, font=f, fill=(0, 0, 0, 90))  # soft drop shadow
    d.text((x, y), kicker, font=f, fill=(*th['kicker_fg'], 255))
    text = text.upper().strip()
    f2 = fit_font(text, MONT_XB_ITALIC, OUT_W - 60, 66, 30)
    bb = f2.getbbox(text)
    d.text(((OUT_W - (bb[2] - bb[0])) / 2 - bb[0], sy - ky + (sh_ - (bb[3] - bb[1])) / 2 - bb[1]), text,
           font=f2, fill=(*th['sub_fg'], 255))
    return np.array(img)


@lru_cache(maxsize=16)
def chip(text: str, theme_name: str, size: int = 32):
    from PIL import Image, ImageDraw

    th = THEMES.get(theme_name, THEMES['brasil'])
    f = font(size, MONT_BLACK)
    bb = f.getbbox(text)
    w, h = int(bb[2] - bb[0]) + 44, int(bb[3] - bb[1]) + 26
    img = Image.new('RGBA', (OUT_W, h), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.rounded_rectangle((36, 0, 36 + w, h), radius=h // 2, fill=(*th['chip_bg'], 240))
    d.text((36 + 22 - bb[0], 13 - bb[1]), text, font=f, fill=(*th['chip_fg'], 255))
    return np.array(img)


@lru_cache(maxsize=8)
def source_tag(text: str):
    from PIL import Image, ImageDraw

    f = font(26, MONT_XB)
    img = Image.new('RGBA', (OUT_W, 44), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.text((40, 6), text, font=f, fill=(255, 255, 255, 235), stroke_width=3, stroke_fill=(0, 0, 0, 170))
    return np.array(img)


def cover_crop(img: np.ndarray, w: int, h: int, cx: float = 0.5, cy: float = 0.5) -> np.ndarray:
    import cv2

    ih, iw = img.shape[:2]
    scale = max(w / iw, h / ih)
    cw, ch = int(round(w / scale)), int(round(h / scale))
    x = int(min(max(cx * iw - cw / 2, 0), iw - cw))
    y = int(min(max(cy * ih - ch / 2, 0), ih - ch))
    return cv2.resize(img[y:y + ch, x:x + cw], (w, h), interpolation=cv2.INTER_LANCZOS4)


def top_image(src: Path, plan: list[dict], sw: int, sh: int, override: str | None) -> np.ndarray:
    """Top panel: a user image if given, else the sharpest big-face frame of the answer."""
    import cv2

    w, h = OUT_W, ALERTA_IMG_H
    if override and Path(override).is_file():
        img = cv2.imread(override)
        if img is not None:
            return sharpen(cover_crop(img, w, h, 0.5, 0.4))
    best = None
    for p in plan:
        if p.get('role') == 'question':
            continue
        for f in p['frames']:
            for face in f['faces']:
                if best is None or face.size > best[1].size:
                    best = (p['start'] + f['t'], face)
    if best is None:
        frame = grab_frame(src, plan[-1]['start'] + plan[-1]['dur'] / 2, sw, sh)
        return cover_crop(frame, w, h)
    t, face = best
    frame = grab_frame(src, t, sw, sh)
    aspect = w / h
    crop_h = min(sh, max(0.45 * sh, face.size * sh * 2.6))
    crop_w = min(sw, crop_h * aspect)
    crop_h = crop_w / aspect
    x = int(min(max(face.cx * sw - crop_w / 2, 0), sw - crop_w))
    # Broadcasters put logos in the top corners: start below them when the crop allows it.
    y_min = min(0.08 * sh, sh - crop_h)
    y = int(min(max(face.cy * sh - crop_h * 0.42, y_min), sh - crop_h))
    out = cv2.resize(frame[y:y + int(crop_h), x:x + int(crop_w)], (w, h), interpolation=cv2.INTER_LANCZOS4)
    return sharpen(out)


def compose_region(frame: np.ndarray, cx: float | None, sw: int, sh: int, rw: int, rh: int,
                   usable_h: int) -> np.ndarray:
    """Fills an rw x rh region from the top `usable_h` rows of the frame (drops a bottom ticker)."""
    import cv2

    src = frame[:usable_h]
    if cx is not None:
        return sharpen(cover_crop(src, rw, rh, cx, 0.5))
    bg = cv2.GaussianBlur(cover_crop(src, rw, rh), (0, 0), 30)
    bg = (bg * 0.6).astype(np.uint8)
    fh = int(rw * usable_h / sw)
    fg = cv2.resize(src, (rw, fh), interpolation=cv2.INTER_AREA)
    y = (rh - fh) // 2
    bg[y:y + fh] = fg
    return bg


# --- render -----------------------------------------------------------------

def part_bounds(transcript: dict, start_id: int, end_id: int, words: list[dict],
                skip_start: int = 0, skip_end: int = 0) -> tuple[float, float]:
    """Sentence range -> (start, end) seconds with natural padding into the silence around it."""
    sents = transcript['sentences']
    a, b = sents[start_id], sents[end_id]
    i0 = a['w0'] + max(0, min(skip_start, a['w1'] - a['w0']))
    i1 = b['w1'] - max(0, min(skip_end, b['w1'] - b['w0']))
    first, last = words[i0], words[i1]
    prev_end = words[i0 - 1]['end'] if i0 > 0 else 0
    next_start = words[i1 + 1]['start'] if i1 + 1 < len(words) else last['end'] + 1
    start = first['start'] - min(0.3, max(0.0, first['start'] - prev_end - 0.05))
    end = last['end'] + min(0.7, max(0.0, next_start - last['end'] - 0.05))
    return max(0.0, start), end


def apply_sentence_fixes(transcript: dict, fixes: dict) -> list[dict]:
    """Returns the word list with proofread sentence texts aligned onto the original timings."""
    import difflib

    words = [dict(w) for s in transcript['segments'] for w in s['words']]
    for sid, text in sorted((fixes or {}).items(), key=lambda kv: int(kv[0])):
        sent = transcript['sentences'][int(sid)]
        old = words[sent['w0']:sent['w1'] + 1]
        new = str(text).split()
        if not new:
            continue
        out = []
        sm = difflib.SequenceMatcher(a=[w['w'].lower() for w in old], b=[w.lower() for w in new], autojunk=False)
        for op, i1, i2, j1, j2 in sm.get_opcodes():
            if op == 'equal':
                out += [{**old[i1 + k], 'w': new[j1 + k]} for k in range(i2 - i1)]
            elif op in ('replace', 'insert'):
                if i2 > i1:
                    t0, t1 = old[i1]['start'], old[i2 - 1]['end']
                else:  # insertion: borrow the gap before the next word
                    t0 = old[i1 - 1]['end'] if i1 > 0 else old[0]['start']
                    t1 = old[i1]['start'] if i1 < len(old) else old[-1]['end']
                n = j2 - j1
                step = (t1 - t0) / n if n else 0
                out += [{'w': new[j1 + k], 'start': round(t0 + k * step, 2), 'end': round(t0 + (k + 1) * step, 2)}
                        for k in range(n)]
            # 'delete': drop the words
        words[sent['w0']:sent['w1'] + 1] = out
        # keep later sentence indexes valid
        shift = len(out) - len(old)
        if shift:
            for other in transcript['sentences'][int(sid) + 1:]:
                other['w0'] += shift
                other['w1'] += shift
        sent['w1'] = sent['w0'] + len(out) - 1
    return words


def trim_micro_shots(frames: list[dict], words_rel: list[dict], dur: float, limit: float = 0.8) -> tuple[float, float]:
    """Drops a camera cut that flashes at the very start/end of a part, if no speech is lost."""
    head, tail = 0.0, dur
    if not frames:
        return head, tail
    first_shot, last_shot = frames[0]['shot'], frames[-1]['shot']
    cut_in = next((f['t'] for f in frames if f['shot'] != first_shot), None)
    if cut_in is not None and cut_in < limit and (not words_rel or words_rel[0]['start'] >= cut_in - 0.05):
        head = cut_in
    cut_out = next((f['t'] for f in reversed(frames) if f['shot'] != last_shot), None)
    if cut_out is not None:
        cut_out += 1 / ANALYSIS_FPS
        if dur - cut_out < limit and (not words_rel or words_rel[-1]['end'] <= cut_out + 0.05):
            tail = cut_out
    return head, tail


def render(workdir: Path, transcript: dict, parts: list[dict], out_path: Path, *,
           context_text: str | None = None, cover_band: list[float] | None = None,
           source_label: str = '', sentence_fixes: dict | None = None, brand: str = BRAND_NAME,
           template: str = 'alerta', headline_kicker: str = '', headline_text: str = '',
           theme: str = 'brasil', top_image_path: str | None = None) -> dict:
    """parts: [{start_id, end_id, role: 'question'|'answer', skip_words_start?, skip_words_end?}] in order."""
    src = find(workdir, 'video')
    audio = find(workdir, 'audio')
    meta = probe(src)
    sw, sh, fps = meta['width'], meta['height'], meta['fps']
    transcript = json.loads(json.dumps(transcript))  # fixes mutate sentence word indexes
    words = apply_sentence_fixes(transcript, sentence_fixes or {})

    # Resolve each part: times, analysis, crop plan, captions (relative to part start).
    plan = []
    for p in parts:
        start, end = part_bounds(transcript, int(p['start_id']), int(p['end_id']), words,
                                 int(p.get('skip_words_start') or 0), int(p.get('skip_words_end') or 0))
        dur = end - start
        frames = analyze(src, start, dur, sw, sh)
        rel = [{'w': w['w'], 'start': w['start'] - start, 'end': w['end'] - start}
               for w in words if w['end'] > start and w['start'] < end]
        head, tail = trim_micro_shots(frames, rel, dur)
        if head or tail < dur:
            frames = [{**f, 't': f['t'] - head} for f in frames if head <= f['t'] < tail]
            rel = [{**w, 'start': w['start'] - head, 'end': w['end'] - head} for w in rel]
            start, end = start + head, start + tail
            dur = end - start
        plan.append({**p, 'start': start, 'end': end, 'dur': dur, 'crops': plan_crops(frames, sw, sh),
                     'words': rel, 'shots': len({f['shot'] for f in frames}), 'frames': frames})

    total = sum(p['dur'] for p in plan)
    if total > 180:
        raise PipelineError('INVALID_PARAMS', f'clip too long: {total:.1f}s')

    # Caption timeline on the output clock.
    timeline = []  # (t0, t1, words tuple, active index, highlight)
    offset = 0.0
    for p in plan:
        chunks = caption_chunks(p['words'])
        for ci, ch in enumerate(chunks):
            texts = tuple(w['w'] for w in ch)
            nxt = chunks[ci + 1][0]['start'] if ci + 1 < len(chunks) else p['dur']
            for wi, w in enumerate(ch):
                t1 = ch[wi + 1]['start'] if wi + 1 < len(ch) else min(nxt, w['end'] + 0.4)
                timeline.append((offset + max(0, w['start']), offset + min(t1, p['dur']), texts, wi,
                                 p.get('role') != 'question'))
        p['offset'] = offset
        offset += p['dur']

    # Audio: trim each part from the audio track, short fades at every join, loudness normalize.
    fade_in, fade_out = 0.06, 0.18
    filt = []
    for i, p in enumerate(plan):
        filt.append(f"[1:a]atrim={p['start']:.3f}:{p['end']:.3f},asetpts=PTS-STARTPTS,"
                    f"afade=t=in:d={fade_in},afade=t=out:st={max(0, p['dur'] - fade_out):.3f}:d={fade_out}[a{i}]")
    filt.append(''.join(f'[a{i}]' for i in range(len(plan))) + f'concat=n={len(plan)}:v=0:a=1,'
                'loudnorm=I=-14:TP=-1.5:LRA=11,aresample=48000[aout]')

    out_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = out_path.with_suffix('.tmp.mp4')
    enc = subprocess.Popen([
        FFMPEG, '-v', 'error', '-y',
        '-f', 'rawvideo', '-pix_fmt', 'bgr24', '-s', f'{OUT_W}x{OUT_H}', '-r', f'{fps}', '-i', '-',
        '-i', str(audio),
        '-filter_complex', ';'.join(filt),
        '-map', '0:v', '-map', '[aout]',
        '-c:v', 'libx264', '-preset', 'medium', '-crf', '17', '-profile:v', 'high', '-pix_fmt', 'yuv420p',
        '-g', str(int(round(fps * 2))), '-c:a', 'aac', '-b:a', '192k',
        '-movflags', '+faststart', str(tmp),
    ], stdin=subprocess.PIPE, stderr=subprocess.PIPE)

    alerta = template == 'alerta'
    if alerta:
        if not headline_kicker or not headline_text:
            raise PipelineError('INVALID_PARAMS', 'alerta template needs headline_kicker and headline_text')
        # Static parts of the frame are built once.
        base = np.zeros((OUT_H, OUT_W, 3), np.uint8)
        base[:ALERTA_IMG_H] = top_image(src, plan, sw, sh, top_image_path)
        grad = np.linspace(0, 0.55, 220)[:, None, None]  # darken the image bottom for the source tag
        base[ALERTA_IMG_H - 400:ALERTA_IMG_H - 180] = (base[ALERTA_IMG_H - 400:ALERTA_IMG_H - 180] * (1 - grad)).astype(np.uint8)
        base[ALERTA_IMG_H - 180:ALERTA_IMG_H] = (base[ALERTA_IMG_H - 180:ALERTA_IMG_H] * 0.45).astype(np.uint8)
        blend(base, chip(brand.upper(), theme), 40)
        if source_label:
            blend(base, source_tag(f'FONTE: {source_label.upper()}'), ALERTA_KICKER[0] - 56)
        bars = headline_bars(headline_kicker, headline_text, theme)
        usable_h = int(cover_band[0] * sh) if cover_band else sh
        region_h = OUT_H - ALERTA_VIDEO_Y
    brand_img = branding_image(brand)
    ctx_img = context_card(context_text) if (context_text and not alerta) else None
    has_question = any(p.get('role') == 'question' for p in plan)
    ctx_until = plan[0]['dur'] if has_question else min(6.0, total)
    n_total = 0
    fit_frames = 0
    try:
        for p in plan:
            ci = 0
            crops = p['crops']
            for n, frame in enumerate(read_frames(src, p['start'], p['dur'], sw, sh)):
                t = n / fps
                if t >= p['dur']:
                    break
                tg = p['offset'] + t
                d = next((c for c in crops if c['t0'] <= t < c['t1']), crops[-1] if crops else {'cx': None})
                if d['cx'] is None:
                    fit_frames += 1
                if alerta:
                    out = base.copy()
                    out[ALERTA_VIDEO_Y:] = compose_region(frame, d['cx'], sw, sh, OUT_W, region_h, usable_h)
                    blend(out, bars, ALERTA_KICKER[0])
                    cap_y = ALERTA_CAPTION_Y
                    if p.get('role') == 'question':
                        blend(out, label_pill('PERGUNTA'), cap_y - 80)
                    while ci < len(timeline) and timeline[ci][1] <= tg:
                        ci += 1
                    if ci < len(timeline) and timeline[ci][0] <= tg:
                        _, _, texts, active, hl = timeline[ci]
                        blend(out, caption_image(texts, active, hl), cap_y)
                    enc.stdin.write(out.tobytes())
                    n_total += 1
                    continue
                out, (fy, fh) = compose(frame, d['cx'], sw, sh)
                if cover_band:
                    y0 = fy + int(cover_band[0] * fh)
                    y1 = fy + int(cover_band[1] * fh)
                    blend(out, cover_banner(y1 - y0, source_label, brand), y0)
                if not cover_band:  # without a bottom banner, the brand goes on top
                    blend(out, brand_img, 60)
                if ctx_img is not None and tg < ctx_until:
                    fade = min(1.0, (ctx_until - tg) / 0.3)
                    blend(out, ctx_img, 190 if not cover_band else 90, opacity=fade)
                # In the fit layout the source frame (and its banner) sits mid-screen: text goes below it.
                cap_y = CAPTION_Y if fh == OUT_H else max(CAPTION_Y, fy + fh + 100)
                if p.get('role') == 'question':
                    blend(out, label_pill('PERGUNTA'), cap_y - 80)
                while ci < len(timeline) and timeline[ci][1] <= tg:
                    ci += 1
                if ci < len(timeline) and timeline[ci][0] <= tg:
                    _, _, texts, active, hl = timeline[ci]
                    blend(out, caption_image(texts, active, hl), cap_y)
                enc.stdin.write(out.tobytes())
                n_total += 1
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
        'duration': round(out_meta['duration'], 2),
        'frames': n_total,
        'fit_ratio': round(fit_frames / max(1, n_total), 3),
        'cover_band': cover_band,
        'template': template,
        'source_video': src.name,
        'parts': [{'role': p.get('role', 'answer'), 'start': round(p['start'], 2), 'end': round(p['end'], 2),
                   'shots': p['shots'],
                   'crop_plan': [{**c, 't0': round(c['t0'], 2), 't1': round(c['t1'], 2),
                                  'cx': None if c['cx'] is None else round(c['cx'], 3)} for c in p['crops']]}
                  for p in plan],
    }
