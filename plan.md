# Plan: QR + selfie wedding photo finder

## Goal
The photographer uploads a wedding's photos to a Google Drive folder. The system creates a QR code for that folder. A guest scans it, takes a selfie, and gets **only the photos their face appears in**, to view or download.

## Scope split
| Photographer (manual) | System (automated) |
|---|---|
| Upload photos to a Drive folder | Index every face in the folder |
| Share folder with the service account | Generate the event link + QR card |
| Create the event in /admin, print the QR | Match guest selfies → personal gallery |
| | Downloads (single + ZIP), auto-expiry |

## Decisions
| Area | Choice | Why |
|---|---|---|
| Face model | DeepFace **Facenet512** | Free (MIT). InsightFace weights are non-commercial only, which is risky for a photography business |
| Face detector | **YuNet** (OpenCV) | 0.2 s/photo vs ~5 s for RetinaFace, with equal or better matches in testing |
| Matching | Cosine similarity, threshold **0.60** | Test set: same person 0.64–0.77, others ≤ 0.47 |
| Storage | **SQLite** + numpy | ≤ ~10k faces per wedding, so search takes milliseconds; no extra DB server to run |
| Drive access | Service account, read-only | Folder stays private; guests only get matched photos via the app |
| Frontend | Plain HTML/JS served by FastAPI | No build step; works on any phone browser |
| Hosting | Laptop for testing → **Oracle Cloud Free Tier** (Docker + Caddy HTTPS) | Always on, ₹0. A laptop must stay awake for guests to reach it |

## Expected load
~10 weddings/month × 500–1,000 photos: ~5–10 min indexing per wedding, ~1 GB thumbnails per month (auto-deleted after 60 days by default), < 2 s per selfie match.

## Status
- [x] Indexer (Drive + local folder, incremental sync, retries, crash resume)
- [x] Guest flow: consent, optional PIN, selfie, gallery, lightbox, download, ZIP, add-another-selfie
- [x] Admin: create / sync / PIN / extend / delete, live progress, QR image + printable card
- [x] Privacy: selfies not stored, auto-expiry, rate limit, unguessable links
- [x] CLI threshold tuning tool, Dockerfile, docker-compose with HTTPS
- [ ] Test with a real Google Drive folder + real wedding photos (needs service-account.json)
- [ ] Tune MATCH_THRESHOLD on a real wedding

## Possible next steps
- Watermark on downloads, studio branding on the guest page
- "Browse people" clustering for the couple
- WhatsApp share button
