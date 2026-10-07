"""Face identification against reference photos (OpenCV YuNet detector + SFace embeddings).

Reference photos live in ASSETS/people/<slug>/*.jpg (2-3 clear, frontal photos are enough).
Used to pick thumbnails / top images that actually show the subject of the video.
"""

from __future__ import annotations

import re
import unicodedata
from functools import lru_cache
from pathlib import Path

import numpy as np

import pipeline as P

ASSETS = Path('/Volumes/MacNVMe/pavanatto-cuts/assets')
MATCH_THRESHOLD = 0.40  # SFace cosine similarity; 0.363 is OpenCV's suggested threshold


def slugify(name: str) -> str:
    s = unicodedata.normalize('NFD', name).encode('ascii', 'ignore').decode().lower()
    return re.sub(r'[^a-z0-9]+', '-', s).strip('-')


@lru_cache(maxsize=1)
def recognizer():
    import cv2

    return cv2.FaceRecognizerSF.create(str(P.CACHE / 'face_recognition_sface_2021dec.onnx'), '')


@lru_cache(maxsize=4)
def detector(w: int, h: int):
    import cv2

    return cv2.FaceDetectorYN.create(str(P.CACHE / 'face_detection_yunet_2023mar.onnx'), '', (w, h), 0.7, 0.3, 50)


def faces_with_features(img: np.ndarray) -> list[tuple[np.ndarray, np.ndarray]]:
    """[(detection row [x, y, w, h, landmarks..., score], normalized feature)] for faces >= 40px."""
    h, w = img.shape[:2]
    _, dets = detector(w, h).detect(img)
    out = []
    for d in dets if dets is not None else []:
        if d[2] < 40 or d[3] < 40:
            continue
        aligned = recognizer().alignCrop(img, d)
        f = recognizer().feature(aligned).flatten()
        out.append((d, f / (np.linalg.norm(f) + 1e-9)))
    return out


def reference(subject: str) -> np.ndarray | None:
    """Mean embedding of the subject's reference photos (cached as ref.npy)."""
    import cv2

    folder = ASSETS / 'people' / slugify(subject)
    photos = sorted(p for p in folder.glob('*') if p.suffix.lower() in ('.jpg', '.jpeg', '.png', '.webp'))
    if not photos:
        return None
    cache = folder / 'ref.npy'
    if cache.exists() and cache.stat().st_mtime >= max(p.stat().st_mtime for p in photos):
        return np.load(cache)
    feats = []
    for p in photos:
        img = cv2.imread(str(p))
        if img is None:
            continue
        found = faces_with_features(img)
        if found:  # the biggest face of each reference photo
            feats.append(max(found, key=lambda x: x[0][2] * x[0][3])[1])
    if not feats:
        return None
    ref = np.mean(feats, axis=0)
    ref /= np.linalg.norm(ref)
    np.save(cache, ref)
    return ref


def find_subject(src: Path, ranges: list[tuple[float, float]], ref: np.ndarray, sw: int, sh: int,
                 step: float = 1.0, max_samples: int = 240) -> list[dict]:
    """Samples the ranges and returns frames where the subject's face is recognized.

    Each hit: {t, cx, cy, size (fractions), sim}.
    """
    total = sum(d for _, d in ranges)
    step = max(step, total / max_samples)
    hits = []
    for start, dur in ranges:
        t = 0.3
        while t < dur:
            frame = P.grab_frame(src, start + t, sw, sh)
            if frame is not None:
                for d, f in faces_with_features(frame):
                    sim = float(np.dot(f, ref))
                    if sim >= MATCH_THRESHOLD:
                        x, y, w, h = d[:4]
                        hits.append({'t': start + t, 'cx': (x + w / 2) / sw, 'cy': (y + h / 2) / sh,
                                     'size': h / sh, 'sim': round(sim, 3)})
            t += step
    return hits


def best_subject_frame(src: Path, ranges, subject: str, sw: int, sh: int):
    """(time, Face-like) of the clearest subject appearance, or None (no reference / not found)."""
    ref = reference(subject) if subject else None
    if ref is None:
        return None
    hits = find_subject(src, ranges, ref, sw, sh)
    if not hits:
        return None
    h = max(hits, key=lambda x: x['size'] * (0.5 + x['sim']))
    # face size from YuNet box (eyes-to-chin) is ~80% of MediaPipe's full-face height
    return h['t'], P.Face(cx=h['cx'], cy=h['cy'], size=h['size'] * 1.25, jaw=0.0)
