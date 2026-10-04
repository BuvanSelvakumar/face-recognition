"""All settings come from environment variables (or a .env file in the project root)."""
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _load_dotenv() -> None:
    env_file = ROOT / ".env"
    if not env_file.exists():
        return
    for line in env_file.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


_load_dotenv()


def _get(name: str, default: str) -> str:
    return os.environ.get(name, default)


# Public URL guests reach (goes inside the QR code), e.g. https://photos.example.com
PUBLIC_BASE_URL = _get("PUBLIC_BASE_URL", "http://localhost:8000").rstrip("/")

# Admin login (HTTP basic auth on /admin and /api/admin/*)
ADMIN_USERNAME = _get("ADMIN_USERNAME", "admin")
ADMIN_PASSWORD = _get("ADMIN_PASSWORD", "change-me")

DATA_DIR = (ROOT / _get("DATA_DIR", "data")).resolve()
DB_PATH = DATA_DIR / "app.db"
MEDIA_DIR = DATA_DIR / "media"

# Google service-account JSON key (share each wedding Drive folder with its email)
GOOGLE_SERVICE_ACCOUNT_FILE = str((ROOT / _get("GOOGLE_SERVICE_ACCOUNT_FILE", "service-account.json")).resolve())

# Allow indexing a folder on this machine's disk (handy when running on your laptop)
ALLOW_LOCAL_FOLDERS = _get("ALLOW_LOCAL_FOLDERS", "true").lower() == "true"

# Face model settings (DeepFace)
FACE_MODEL = _get("FACE_MODEL", "Facenet512")
FACE_DETECTOR = _get("FACE_DETECTOR", "yunet")  # yunet (fast, ~0.2s/photo) | retinaface (~5s/photo) | mtcnn
MIN_FACE_CONFIDENCE = float(_get("MIN_FACE_CONFIDENCE", "0.90"))
MIN_FACE_PX = int(_get("MIN_FACE_PX", "40"))  # ignore tiny background faces

# Cosine similarity a face needs to count as "same person". Raise for fewer wrong
# photos, lower to catch more. Use `python -m app.cli match` to tune on a real event.
MATCH_THRESHOLD = float(_get("MATCH_THRESHOLD", "0.60"))

# Image sizes kept on disk
DETECT_MAX_SIDE = int(_get("DETECT_MAX_SIDE", "1800"))
PREVIEW_MAX_SIDE = int(_get("PREVIEW_MAX_SIDE", "1600"))
THUMB_MAX_SIDE = int(_get("THUMB_MAX_SIDE", "400"))

DEFAULT_EVENT_DAYS = int(_get("DEFAULT_EVENT_DAYS", "60"))
GUEST_SESSION_DAYS = int(_get("GUEST_SESSION_DAYS", "30"))
MATCH_RATE_LIMIT_PER_MIN = int(_get("MATCH_RATE_LIMIT_PER_MIN", "15"))

DATA_DIR.mkdir(parents=True, exist_ok=True)
MEDIA_DIR.mkdir(parents=True, exist_ok=True)
