"""Long-form (16:9) renderer: cold open + chaptered segments, title card, captions, thumbnail.

Reuses the transcript/word/analysis helpers from pipeline.py; output is 1920x1080.
"""

from __future__ import annotations

import subprocess
from functools import lru_cache
from pathlib import Path

import numpy as np

import faceid
import pipeline as P

W, H = 1920, 1080
TITLE_CARD_SECONDS = 3.5
CHAPTER_SECONDS = (0.4, 4.6)
END_CARD_SECONDS = 4.0


# --- overlays -------------------------------------------------------------------

@lru_cache(maxsize=16)
def title_card(kicker: str, text: str, theme: str, brand: str):
    """Centered impact bar + headline bar (the Short headline, scaled for 16:9)."""
    from PIL import Image, ImageDraw

    th = P.THEMES.get(theme, P.THEMES['brasil'])
    img = Image.new('RGBA', (W, H), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    kh, sh_ = 200, 110
    y0 = (H - kh - sh_) // 2
    d.rectangle((0, y0, W, y0 + kh), fill=(*th['kicker_bg'], 255))
    d.rectangle((0, y0 + kh, W, y0 + kh + sh_), fill=(*th['sub_bg'], 255))
    f = P.fit_font(kicker.upper(), P.MONT_BLACK, W - 160, 176, 60)
    bb = f.getbbox(kicker.upper())
    x, y = (W - (bb[2] - bb[0])) / 2 - bb[0], y0 + (kh - (bb[3] - bb[1])) / 2 - bb[1]
    d.text((x + 6, y + 7), kicker.upper(), font=f, fill=(0, 0, 0, 90))
    d.text((x, y), kicker.upper(), font=f, fill=(*th['kicker_fg'], 255))
    t = text.upper()
    f2 = P.fit_font(t, P.MONT_XB_ITALIC, W - 140, 76, 30)
    bb = f2.getbbox(t)
    d.text(((W - (bb[2] - bb[0])) / 2 - bb[0], y0 + kh + (sh_ - (bb[3] - bb[1])) / 2 - bb[1]), t, font=f2,
           fill=(*th['sub_fg'], 255))
    fb = P.font(40, P.MONT_BLACK)
    bb = fb.getbbox(brand.upper())
    d.text(((W - (bb[2] - bb[0])) / 2, y0 - 80), brand.upper(), font=fb, fill=(255, 255, 255, 235),
           stroke_width=3, stroke_fill=(0, 0, 0, 160))
    return np.array(img)


@lru_cache(maxsize=64)
def chapter_tag(number: int, title: str, theme: str):
    """Lower-third chapter tag: number box + title bar, bottom-left."""
    from PIL import Image, ImageDraw

    th = P.THEMES.get(theme, P.THEMES['brasil'])
    title = title.upper()
    f = P.fit_font(title, P.MONT_BLACK, 1150, 52, 28)
    bb = f.getbbox(title)
    tw, th_ = int(bb[2] - bb[0]), int(bb[3] - bb[1])
    box = 96
    img = Image.new('RGBA', (W, box + 12), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    x0 = 70
    d.rectangle((x0, 0, x0 + box, box), fill=(*th['sub_bg'], 255))
    fn = P.font(60, P.MONT_BLACK)
    nb = fn.getbbox(str(number))
    d.text((x0 + (box - (nb[2] - nb[0])) / 2 - nb[0], (box - (nb[3] - nb[1])) / 2 - nb[1]), str(number), font=fn,
           fill=(*th['sub_fg'], 255))
    d.rectangle((x0 + box, 0, x0 + box + tw + 60, box), fill=(*th['kicker_bg'], 245))
    d.text((x0 + box + 30 - bb[0], (box - th_) / 2 - bb[1]), title, font=f, fill=(*th['kicker_fg'], 255))
    return np.array(img)


@lru_cache(maxsize=8)
def corner_tags(brand: str, source_label: str, theme: str):
    """Brand chip (top-right) and source tag (top-left) on a transparent full-width strip."""
    from PIL import Image, ImageDraw

    th = P.THEMES.get(theme, P.THEMES['brasil'])
    img = Image.new('RGBA', (W, 90), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    fb = P.font(30, P.MONT_BLACK)
    bb = fb.getbbox(brand.upper())
    w, h = int(bb[2] - bb[0]) + 40, int(bb[3] - bb[1]) + 24
    x = W - 50 - w
    d.rounded_rectangle((x, 30, x + w, 30 + h), radius=h // 2, fill=(*th['chip_bg'], 215))
    d.text((x + 20 - bb[0], 30 + 12 - bb[1]), brand.upper(), font=fb, fill=(*th['chip_fg'], 255))
    if source_label:
        fs = P.font(24, P.MONT_XB)
        d.text((50, 38), f'FONTE: {source_label.upper()}', font=fs, fill=(255, 255, 255, 220),
               stroke_width=3, stroke_fill=(0, 0, 0, 150))
    return np.array(img)


@lru_cache(maxsize=4096)
def caption_16x9(words: tuple[str, ...], active: int, highlight: bool = True):
    from PIL import Image, ImageDraw

    up = [w.upper() for w in words]
    for size in (58, 52, 46, 40):
        f = P.font(size)
        lines = P.wrap(up, f, 1500)
        if lines:
            break
    else:
        lines = [list(range(len(up)))]
    line_h = int(size * 1.3)
    img = Image.new('RGBA', (W, line_h * len(lines) + 30), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    space = f.getlength(' ')
    for li, idxs in enumerate(lines):
        total = sum(f.getlength(up[i]) for i in idxs) + space * (len(idxs) - 1)
        x = (W - total) / 2
        for i in idxs:
            color = P.YELLOW if (highlight and i == active) else (255, 255, 255, 255)
            d.text((x, 15 + li * line_h), up[i], font=f, fill=color, stroke_width=6, stroke_fill=(0, 0, 0, 255))
            x += f.getlength(up[i]) + space
    return np.array(img)


@lru_cache(maxsize=4)
def end_card(brand: str, theme: str):
    from PIL import Image, ImageDraw

    th = P.THEMES.get(theme, P.THEMES['brasil'])
    img = Image.new('RGBA', (W, 150), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    text = f'INSCREVA-SE NO {brand.upper()}'
    f = P.fit_font(text, P.MONT_BLACK, W - 400, 62, 30)
    bb = f.getbbox(text)
    w = int(bb[2] - bb[0]) + 90
    x = (W - w) // 2
    d.rounded_rectangle((x, 20, x + w, 130), radius=55, fill=(*th['kicker_bg'], 240))
    d.text((x + 45 - bb[0], 75 - (bb[3] - bb[1]) / 2 - bb[1]), text, font=f, fill=(255, 255, 255, 255))
    return np.array(img)


@lru_cache(maxsize=16)
def cover_bar(height: int, brand: str, source_label: str):
    from PIL import Image, ImageDraw

    height = max(24, height)
    img = Image.new('RGBA', (W, height), (14, 14, 14, 250))
    d = ImageDraw.Draw(img)
    d.rectangle((0, 0, W, max(3, height // 20)), fill=P.YELLOW)
    text = brand.upper() + (f'  •  FONTE: {source_label.upper()}' if source_label else '')
    f = P.fit_font(text, P.MONT_BLACK, W - 120, min(54, int(height * 0.45)), 14)
    bb = f.getbbox(text)
    d.text(((W - (bb[2] - bb[0])) / 2 - bb[0], (height - (bb[3] - bb[1])) / 2 - bb[1] + height // 30), text,
           font=f, fill=(255, 255, 255, 255))
    return np.array(img)


def blend_x(frame: np.ndarray, rgba: np.ndarray, x: int, y: int, opacity: float = 1.0) -> None:
    h = min(rgba.shape[0], frame.shape[0] - y)
    w = min(rgba.shape[1], frame.shape[1] - x)
    if h <= 0 or w <= 0:
        return
    region = frame[y:y + h, x:x + w]
    a = rgba[:h, :w, 3:4].astype(np.float32) / 255 * opacity
    region[:] = (rgba[:h, :w, 2::-1].astype(np.float32) * a + region.astype(np.float32) * (1 - a)).astype(np.uint8)


# --- thumbnail -------------------------------------------------------------------

def thumbnail(src: Path, face_time: float, face, sw: int, sh: int, kicker: str, text: str, theme: str,
              brand: str, out: Path) -> Path:
    """1280x720: face on the right, impact bar + big headline on the left."""
    import cv2
    from PIL import Image, ImageDraw

    th = P.THEMES.get(theme, P.THEMES['brasil'])
    tw, tht = 1280, 720
    frame = P.grab_frame(src, face_time, sw, sh)
    cx = face.cx if face else 0.5
    cy = face.cy if face else 0.45
    if face:
        crop_h = min(sh, max(0.5 * sh, face.size * sh * 2.4))
    else:
        crop_h = sh
    crop_w = min(sw, crop_h * tw / tht)
    crop_h = crop_w * tht / tw
    # face sits in the right third
    x = int(min(max(cx * sw - crop_w * 0.68, 0), sw - crop_w))
    y = int(min(max(cy * sh - crop_h * 0.42, 0), sh - crop_h))
    bg = cv2.resize(frame[y:y + int(crop_h), x:x + int(crop_w)], (tw, tht), interpolation=cv2.INTER_LANCZOS4)
    bg = P.sharpen(bg)
    grad = np.linspace(0.85, 0.0, int(tw * 0.62))[None, :, None]
    bg[:, :grad.shape[1]] = (bg[:, :grad.shape[1]] * (1 - grad)).astype(np.uint8)
    img = Image.fromarray(bg[:, :, ::-1]).convert('RGBA')
    d = ImageDraw.Draw(img)
    k = kicker.upper()
    fk = P.fit_font(k, P.MONT_BLACK, 640, 96, 40)
    kb = fk.getbbox(k)
    d.rectangle((0, 60, int(kb[2] - kb[0]) + 90, 60 + int(kb[3] - kb[1]) + 50), fill=(*th['kicker_bg'], 255))
    d.text((45 - kb[0], 85 - kb[1]), k, font=fk, fill=(*th['kicker_fg'], 255))
    words = text.upper().split()
    for size in (96, 88, 80, 72, 64, 56):
        f = P.font(size, P.MONT_BLACK)
        lines = P.wrap(words, f, 700, max_lines=3)
        if lines:
            break
    else:
        lines = [list(range(len(words)))]
    y0 = 60 + int(kb[3] - kb[1]) + 90
    for li, idxs in enumerate(lines):
        line = ' '.join(words[i] for i in idxs)
        color = (*th['sub_bg'], 255) if li % 2 == 0 else (255, 255, 255, 255)
        d.text((45, y0 + li * int(size * 1.12)), line, font=f, fill=color, stroke_width=6, stroke_fill=(0, 0, 0, 255))
    fb = P.font(30, P.MONT_BLACK)
    d.text((45, tht - 70), brand.upper(), font=fb, fill=(255, 255, 255, 230), stroke_width=3, stroke_fill=(0, 0, 0, 180))
    img.convert('RGB').save(out, quality=92)
    return out


# --- render ----------------------------------------------------------------------

def render(workdir: Path, transcript: dict, parts: list[dict], out_path: Path, *, headline_kicker: str,
           headline_text: str, theme: str = 'brasil', brand: str = P.BRAND_NAME, source_label: str = '',
           sentence_fixes: dict | None = None, cover_band: list[float] | None = None,
           captions: bool = True, thumbnail_time: float | None = None, subject: str = '') -> dict:
    """parts: [{start_id, end_id, role: 'hook'|'segment', chapter?, skip_words_start?, skip_words_end?}]."""
    import json

    src = P.find(workdir, 'video')
    audio = P.find(workdir, 'audio')
    meta = P.probe(src)
    sw, sh, fps = meta['width'], meta['height'], meta['fps']
    transcript = json.loads(json.dumps(transcript))
    words = P.apply_sentence_fixes(transcript, sentence_fixes or {})

    plan = []
    for p in parts:
        start, end = P.part_bounds(transcript, int(p['start_id']), int(p['end_id']), words,
                                   int(p.get('skip_words_start') or 0), int(p.get('skip_words_end') or 0))
        rel = [{'w': w['w'], 'start': w['start'] - start, 'end': w['end'] - start}
               for w in words if w['end'] > start and w['start'] < end]
        plan.append({**p, 'start': start, 'end': end, 'dur': end - start, 'words': rel})
    total = sum(p['dur'] for p in plan)
    if total > 30 * 60:
        raise P.PipelineError('INVALID_PARAMS', f'long-form too long: {total:.0f}s')

    # Output clock: offsets, chapters, caption timeline.
    offset, chapters, timeline, n_seg = 0.0, [], [], 0
    for p in plan:
        p['offset'] = offset
        if p.get('role') == 'segment':
            n_seg += 1
            p['number'] = n_seg
            chapters.append({'title': p.get('chapter') or f'Parte {n_seg}', 'start': round(offset, 2)})
        if captions:
            chunks = P.caption_chunks(p['words'], max_words=6, max_chars=40)
            for ci, ch in enumerate(chunks):
                texts = tuple(w['w'] for w in ch)
                nxt = chunks[ci + 1][0]['start'] if ci + 1 < len(chunks) else p['dur']
                for wi, w in enumerate(ch):
                    t1 = ch[wi + 1]['start'] if wi + 1 < len(ch) else min(nxt, w['end'] + 0.4)
                    timeline.append((offset + max(0, w['start']), offset + min(t1, p['dur']), texts, wi))
        offset += p['dur']
    first_seg = next((p for p in plan if p.get('role') == 'segment'), plan[0])

    # Audio: fades only where the timeline jumps (contiguous chapter splits stay seamless).
    filt = []
    for i, p in enumerate(plan):
        prev_join = i > 0 and abs(plan[i - 1]['end'] - p['start']) < 0.05
        next_join = i + 1 < len(plan) and abs(plan[i + 1]['start'] - p['end']) < 0.05
        fx = f"[1:a]atrim={p['start']:.3f}:{p['end']:.3f},asetpts=PTS-STARTPTS"
        if not prev_join:
            fx += ',afade=t=in:d=0.06'
        if not next_join:
            fx += f",afade=t=out:st={max(0, p['dur'] - 0.25):.3f}:d=0.25"
        filt.append(fx + f'[a{i}]')
    filt.append(''.join(f'[a{i}]' for i in range(len(plan))) + f'concat=n={len(plan)}:v=0:a=1,'
                'loudnorm=I=-14:TP=-1.5:LRA=11,aresample=48000[aout]')

    out_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = out_path.with_suffix('.tmp.mp4')
    enc = subprocess.Popen([
        P.FFMPEG, '-v', 'error', '-y',
        '-f', 'rawvideo', '-pix_fmt', 'bgr24', '-s', f'{W}x{H}', '-r', f'{fps}', '-i', '-',
        '-i', str(audio), '-filter_complex', ';'.join(filt), '-map', '0:v', '-map', '[aout]',
        '-c:v', 'libx264', '-preset', 'medium', '-crf', '18', '-profile:v', 'high', '-pix_fmt', 'yuv420p',
        '-g', str(int(round(fps * 2))), '-c:a', 'aac', '-b:a', '192k', '-movflags', '+faststart', str(tmp),
    ], stdin=subprocess.PIPE, stderr=subprocess.PIPE)

    corners = corner_tags(brand, '' if cover_band else source_label, theme)
    card = title_card(headline_kicker, headline_text, theme, brand)
    endc = end_card(brand, theme)
    cap_y = int(cover_band[0] * H) - 165 if cover_band else int(H * 0.80)
    marks_top = P.detect_static_marks(workdir)  # keep a creator watermark readable: captions go above it
    if marks_top is not None and marks_top * H < cap_y + 150:
        cap_y = max(int(H * 0.55), int(marks_top * H) - 160)
    n_total = 0
    try:
        for p in plan:
            ci = 0
            for n, frame in enumerate(P.read_frames(src, p['start'], p['dur'], W, H, fit=True)):
                t = n / fps
                if t >= p['dur']:
                    break
                tg = p['offset'] + t
                out = frame.copy()
                if cover_band:
                    y0, y1 = int(cover_band[0] * H), int(cover_band[1] * H)
                    blend_x(out, cover_bar(y1 - y0, brand, source_label), 0, y0)
                blend_x(out, corners, 0, 0)
                if p is first_seg and t < TITLE_CARD_SECONDS:
                    fade = min(1.0, t / 0.25, (TITLE_CARD_SECONDS - t) / 0.35)
                    out[:] = (out * (1 - 0.45 * fade)).astype(np.uint8)  # dim the frame under the card
                    blend_x(out, card, 0, 0, opacity=fade)
                elif p.get('role') == 'segment' and CHAPTER_SECONDS[0] <= t < CHAPTER_SECONDS[1]:
                    a = min(1.0, (t - CHAPTER_SECONDS[0]) / 0.3, (CHAPTER_SECONDS[1] - t) / 0.3)
                    blend_x(out, chapter_tag(p['number'], p.get('chapter') or f"Parte {p['number']}", theme),
                            0, int(H * 0.66), opacity=a)
                if total - tg < END_CARD_SECONDS:
                    a = min(1.0, (END_CARD_SECONDS - (total - tg)) / 0.4)
                    blend_x(out, endc, 0, int(H * 0.06) + 80, opacity=a)
                if timeline:
                    while ci < len(timeline) and timeline[ci][1] <= tg:
                        ci += 1
                    if ci < len(timeline) and timeline[ci][0] <= tg and not (p is first_seg and t < TITLE_CARD_SECONDS):
                        _, _, texts, active = timeline[ci]
                        blend_x(out, caption_16x9(texts, active), 0, cap_y)
                enc.stdin.write(out.tobytes())
                n_total += 1
    finally:
        enc.stdin.close()
        err = enc.stderr.read().decode()
        enc.wait()
    if enc.returncode != 0:
        raise P.PipelineError('RENDER_FAILED', err[-800:])
    tmp.replace(out_path)

    # Thumbnail: the person talking at the planner's chosen moment (falls back to the first segment).
    # Preferred: the subject recognized by face (reference photos); else the talking face.
    t_thumb = thumbnail_time if thumbnail_time is not None else first_seg['start'] + min(5.0, first_seg['dur'] / 2)
    found = faceid.best_subject_frame(src, [(p['start'], p['dur']) for p in plan], subject, sw, sh)
    found = found or P.speaking_face(src, t_thumb, sw, sh)
    face_t, face = found if found else (t_thumb, None)
    thumb = thumbnail(src, face_t, face, sw, sh, headline_kicker, headline_text, theme, brand,
                      out_path.with_suffix('.jpg'))

    out_meta = P.probe(out_path)
    return {
        'path': str(out_path), 'thumbnail': str(thumb),
        'width': out_meta['width'], 'height': out_meta['height'], 'duration': round(out_meta['duration'], 2),
        'frames': n_total, 'chapters': chapters, 'cover_band': cover_band,
        'parts': [{'role': p.get('role'), 'chapter': p.get('chapter'), 'start': round(p['start'], 2),
                   'end': round(p['end'], 2), 'offset': round(p['offset'], 2)} for p in plan],
    }
