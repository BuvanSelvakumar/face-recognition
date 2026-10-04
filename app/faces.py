"""Face detection + embedding with DeepFace (open source, MIT licensed).

Each face becomes a 512-number vector (Facenet512). Vectors are L2-normalised, so
comparing two faces is just a dot product (cosine similarity, 1.0 = identical)."""
import io
import os
import threading
from dataclasses import dataclass

import numpy as np
from PIL import Image, ImageFile, ImageOps

from . import config

os.environ.setdefault("TF_CPP_MIN_LOG_LEVEL", "3")

try:
    import pillow_heif

    pillow_heif.register_heif_opener()  # iPhone HEIC photos
except ImportError:
    pass

Image.MAX_IMAGE_PIXELS = 200_000_000
ImageFile.LOAD_TRUNCATED_IMAGES = True  # a few missing bytes at the end shouldn't lose the photo

# TensorFlow models are not safe to call from several threads at once.
_model_lock = threading.Lock()


@dataclass
class Face:
    embedding: np.ndarray  # float32, unit length
    x: int
    y: int
    w: int
    h: int
    confidence: float


class NoFaceError(Exception):
    pass


def open_image(data: bytes) -> Image.Image:
    img = Image.open(io.BytesIO(data))
    img = ImageOps.exif_transpose(img)  # respect camera rotation
    if img.mode != "RGB":
        img = img.convert("RGB")
    return img


def resized(img: Image.Image, max_side: int) -> Image.Image:
    if max(img.size) <= max_side:
        return img
    copy = img.copy()
    copy.thumbnail((max_side, max_side), Image.Resampling.LANCZOS)
    return copy


def to_jpeg(img: Image.Image, quality: int = 85) -> bytes:
    buf = io.BytesIO()
    img.save(buf, "JPEG", quality=quality, optimize=True, progressive=True)
    return buf.getvalue()


def detect_faces(img: Image.Image) -> list[Face]:
    from deepface import DeepFace

    small = resized(img, config.DETECT_MAX_SIDE)
    bgr = np.asarray(small)[:, :, ::-1].copy()
    with _model_lock:
        results = DeepFace.represent(
            img_path=bgr,
            model_name=config.FACE_MODEL,
            detector_backend=config.FACE_DETECTOR,
            enforce_detection=False,
            align=True,
        )
    faces: list[Face] = []
    for r in results:
        conf = float(r.get("face_confidence") or 0)
        area = r["facial_area"]
        if conf < config.MIN_FACE_CONFIDENCE:
            continue  # includes DeepFace's "whole image" fallback when no face is found
        if min(area["w"], area["h"]) < config.MIN_FACE_PX:
            continue
        vec = np.asarray(r["embedding"], dtype=np.float32)
        norm = np.linalg.norm(vec)
        if norm == 0:
            continue
        faces.append(Face(vec / norm, int(area["x"]), int(area["y"]), int(area["w"]), int(area["h"]), conf))
    return faces


def selfie_embedding(data: bytes) -> np.ndarray:
    """Embedding of the main (largest) face in a selfie."""
    try:
        img = open_image(data)
    except Exception as e:
        raise NoFaceError("Couldn't read that image. Please try another photo.") from e
    faces = detect_faces(img)
    if not faces:
        raise NoFaceError("No face found. Use a clear, well-lit selfie looking at the camera.")
    return max(faces, key=lambda f: f.w * f.h).embedding


def warm_up() -> None:
    """Load model weights now (they download on first use) so the first guest isn't slow."""
    from deepface import DeepFace

    with _model_lock:
        DeepFace.build_model(config.FACE_MODEL)
    blank = Image.new("RGB", (200, 200), "white")
    detect_faces(blank)
