# #!/usr/bin/env python3
# """
# ForestGuard AI — Android Camera Agent
# ======================================
# Turns any Android phone (with IP Webcam app) into a real edge AI camera.

# QUICK START
# -----------
# Demo mode (no phone needed):
#     DEMO_MODE=true CAMERA_ID=cam_01 python camera_agent.py 

# Real Android phone:
#     PHONE_URL=http://192.168.1.69:8080/shot.jpg \
#     CAMERA_ID=cam_02 \
#     python camera_agent.py

# Multiple cameras (open separate terminals for each):
#     DEMO_MODE=true CAMERA_ID=cam_01 python camera_agent.py
#     PHONE_URL=http://192.168.1.69:8080/shot.jpg CAMERA_ID=cam_02 python camera_agent.py

# ANDROID SETUP (IP Webcam)
# --------------------------
# 1. Install "IP Webcam" from Play Store (by Pavel Khlebovich — top result)
# 2. Open the app
# 3. Scroll all the way down → tap "Start server"
# 4. Your phone shows an IP like http://192.168.1.69:8080
# 5. On your laptop browser, open that URL to verify the feed
# 6. Set PHONE_URL=http://192.168.1.69:8080/shot.jpg and run this script

# Both phone and laptop must be on the same WiFi network.

# ENVIRONMENT VARIABLES
# ---------------------
# PHONE_URL       URL to IP Webcam JPEG endpoint
#                 Default: http://192.168.1.69:8080/shot.jpg
# PHONE_STATUS_URL Optional IP Webcam status endpoint for battery reads
#                 Example: http://192.168.1.69:8080/status.json
#                 Default: auto-try /status.json, /sensors.json, /status
# SERVER_URL      Backend camera-data endpoint
#                 Default: http://localhost:3000/api/camera-data
# CAMERA_ID       Must match a camera ID in the dashboard (cam_01 through cam_05)
#                 Default: cam_01
# CAMERA_SECRET   Shared secret for authentication
#                 Default: forestguard-dev-secret
# POLL_INTERVAL   Seconds between frames
#                 Default: 4
# DEMO_MODE       Set to "true" to run without a phone
#                 Default: false
# BATTERY_FALLBACK Battery % used if phone battery endpoint is unavailable
#                 Default: 85
# YOLO_IMAGE_SIZE  Inference image size for YOLO
#                 Default: 960
# CONFIDENCE_MIN  Minimum confidence threshold (0.0-1.0)
#                 Default: 0.25  (lowered to catch more detections)
# """

# import cv2
# import requests
# import time
# import json
# import random
# import sqlite3
# import os
# import string
# import re
# from datetime import datetime, timezone
# from pathlib import Path
# import numpy as np
# from typing import Dict, List, Optional, Any

# # ── Configuration ─────────────────────────────────────────────────────────────
# PHONE_URL      = os.getenv("PHONE_URL", "http://192.168.1.69:8080/shot.jpg")
# PHONE_STATUS_URL = os.getenv("PHONE_STATUS_URL", "")
# SERVER_URL     = os.getenv("SERVER_URL", "http://localhost:3000/api/camera-data")
# CAMERA_ID      = os.getenv("CAMERA_ID", "cam_01")
# CAMERA_SECRET  = os.getenv("CAMERA_SECRET", "forestguard-dev-secret")
# POLL_INTERVAL  = int(os.getenv("POLL_INTERVAL", "4"))
# CONFIDENCE_MIN = float(os.getenv("CONFIDENCE_MIN", "0.25"))   # lowered to catch more detections
# DEMO_MODE      = os.getenv("DEMO_MODE", "false").lower() in ("true", "1", "yes")
# BATTERY_FALLBACK = int(os.getenv("BATTERY_FALLBACK", "85"))
# YOLO_IMAGE_SIZE = int(os.getenv("YOLO_IMAGE_SIZE", "960"))
# BUFFER_DB      = Path("buffer.db")
# # ──────────────────────────────────────────────────────────────────────────────


# def candidate_server_urls(primary_url: str) -> List[str]:
#     urls = [primary_url]
#     if "localhost" in primary_url:
#         urls.append(primary_url.replace("localhost", "127.0.0.1"))
#     elif "127.0.0.1" in primary_url:
#         urls.append(primary_url.replace("127.0.0.1", "localhost"))

#     if ":3000/" in primary_url:
#         urls.append(primary_url.replace(":3000/", ":5173/"))
#     if ":5173/" in primary_url:
#         urls.append(primary_url.replace(":5173/", ":3000/"))

#     deduped = []
#     for url in urls:
#         if url not in deduped:
#             deduped.append(url)
#     return deduped

# # COCO class names → ForestGuard species mapping
# # YOLOv8 is trained on COCO (80 classes). We map relevant ones.
# COCO_TO_FOREST: Dict[str, Optional[str]] = {
#     "person":   "human",
#     "bird":     "peacock",       # broad approximation
#     "elephant": "elephant",
#     "bear":     "sloth_bear",
#     "deer":     "deer",
#     "cow":      "wild_boar",     # rough stand-in for ungulates
#     "horse":    "deer",
#     "dog":      None,
#     "cat":      None,
#     "car":      None,
#     "truck":    None,
#     "bicycle":  None,
#     "motorcycle": None,
# }

# KNOWN_SPECIES = {
#     "deer", "wild_boar", "rhesus_monkey", "peacock", "leopard",
#     "tiger", "elephant", "rhino", "sloth_bear", "human",
# }

# # Demo mode uses these
# DEMO_ANIMALS = ["deer", "wild_boar", "rhesus_monkey", "peacock", "leopard",
#                 "tiger", "elephant", "rhino", "sloth_bear"]


# # ── Offline buffer (SQLite) ───────────────────────────────────────────────────
# def init_buffer() -> sqlite3.Connection:
#     db = sqlite3.connect(str(BUFFER_DB), check_same_thread=False)
#     db.execute("""
#         CREATE TABLE IF NOT EXISTS queue (
#             id         INTEGER PRIMARY KEY AUTOINCREMENT,
#             payload    TEXT    NOT NULL,
#             created_at REAL    NOT NULL
#         )
#     """)
#     db.commit()
#     return db


# def buffer_payload(db: sqlite3.Connection, payload: Dict[str, Any]):
#     db.execute(
#         "INSERT INTO queue (payload, created_at) VALUES (?, ?)",
#         (json.dumps(payload), time.time()),
#     )
#     db.commit()
#     print(f"  [buffer] Queued offline (total: {db.execute('SELECT COUNT(*) FROM queue').fetchone()[0]})")


# def flush_buffer(db: sqlite3.Connection, headers: dict):
#     rows = db.execute("SELECT id, payload FROM queue ORDER BY id LIMIT 20").fetchall()
#     flushed = 0
#     for row_id, raw in rows:
#         try:
#             posted = False
#             for server_url in candidate_server_urls(SERVER_URL):
#                 try:
#                     r = requests.post(server_url, json=json.loads(raw), headers=headers, timeout=5)
#                     if r.ok:
#                         db.execute("DELETE FROM queue WHERE id = ?", (row_id,))
#                         db.commit()
#                         flushed += 1
#                         posted = True
#                         break
#                 except Exception:
#                     continue
#             if not posted:
#                 break
#         except Exception:
#             break
#     if flushed:
#         print(f"  [buffer] Flushed {flushed} queued events")


# # ── YOLO model ────────────────────────────────────────────────────────────────
# def load_model():
#     try:
#         from ultralytics import YOLO
#         print("[agent] Loading YOLOv8n (downloads ~6 MB on first run)...")
#         m = YOLO("yolov8n.pt")
#         print("[agent] Model loaded.")
#         return m
#     except ImportError:
#         print("[agent] WARNING: ultralytics not installed.")
#         print("[agent]   Install with: pip install ultralytics")
#         print("[agent]   Falling back to demo mode.")
#         return None
#     except Exception as e:
#         print(f"[agent] WARNING: Could not load model: {e}")
#         print("[agent]   Falling back to demo mode.")
#         return None


# def run_inference(model, frame, frame_w: int, frame_h: int) -> List[Dict[str, Any]]:
#     """Run YOLOv8 and return detections with normalized bounding boxes."""
#     results = model.predict(frame, verbose=False, imgsz=YOLO_IMAGE_SIZE)[0]
#     detections = []

#     # ── DEBUG: print ALL raw detections so you can see what YOLO sees ──────
#     raw_count = len(results.boxes)
#     if raw_count == 0:
#         print(f"[yolo] No objects detected at all in this frame")
#     else:
#         print(f"[yolo] Raw detections ({raw_count}):")
#         for box in results.boxes:
#             raw_label = model.names[int(box.cls[0])]
#             conf = float(box.conf[0])
#             print(f"       {raw_label}: {round(conf*100)}% conf")
#     # ────────────────────────────────────────────────────────────────────────

#     for box in results.boxes:
#         conf = float(box.conf[0])
#         raw_label = model.names[int(box.cls[0])]
#         label = COCO_TO_FOREST.get(raw_label, raw_label)

#         if conf < CONFIDENCE_MIN:
#             print(f"[yolo] Skipping {raw_label} ({round(conf*100)}%) — below threshold {round(CONFIDENCE_MIN*100)}%")
#             continue
#         if label is None:
#             continue            # explicitly excluded class (dog, car, etc.)
#         if label not in KNOWN_SPECIES:
#             continue            # unmapped class — skip

#         # Bounding box in pixel coords → normalize to 0-100 percent
#         x1, y1, x2, y2 = box.xyxy[0].tolist()
#         det = {
#             "object":     label,
#             "confidence": round(conf, 3),
#             "box": {
#                 "x":  round(x1 / frame_w * 100, 2),   # left edge %
#                 "y":  round(y1 / frame_h * 100, 2),   # top edge %
#                 "w":  round((x2 - x1) / frame_w * 100, 2),  # width %
#                 "h":  round((y2 - y1) / frame_h * 100, 2),  # height %
#             },
#         }
#         detections.append(det)

#     return detections


# # ── Demo inference ────────────────────────────────────────────────────────────
# def demo_inference() -> Optional[List[Dict[str, Any]]]:
#     if random.random() < 0.55:  # 55% empty frames suppressed
#         return None
#     dets = []
#     if random.random() < 0.15:
#         dets.append({
#             "object": "human",
#             "confidence": round(random.uniform(0.65, 0.95), 2),
#             "box": {"x": round(random.uniform(20, 50), 1), "y": round(random.uniform(10, 30), 1), "w": 14.0, "h": 42.0},
#         })
#     count = 1 + (1 if random.random() < 0.08 else 0)
#     for i in range(count):
#         animal = random.choice(DEMO_ANIMALS)
#         w = 36.0 if animal == "elephant" else 24.0
#         h = 44.0 if animal == "elephant" else 30.0
#         dets.append({
#             "object": animal,
#             "confidence": round(random.uniform(0.60, 0.99), 2),
#             "box": {"x": round(random.uniform(5 + i*25, 30 + i*25), 1), "y": round(random.uniform(20, 50), 1), "w": w, "h": h},
#         })
#     return dets


# # ── Grab frame from Android phone ────────────────────────────────────────────
# def grab_frame(session: requests.Session):
#     resp = session.get(f"{PHONE_URL}?t={int(time.time()*1000)}", timeout=4)
#     resp.raise_for_status()
#     img = np.frombuffer(resp.content, np.uint8)
#     frame = cv2.imdecode(img, cv2.IMREAD_COLOR)
#     if frame is None:
#         raise ValueError("Could not decode image from phone")
#     return frame


# def _coerce_battery_percent(value: Any) -> Optional[int]:
#     if isinstance(value, bool) or value is None:
#         return None
#     if isinstance(value, (int, float)):
#         if 0 <= value <= 1:
#             return int(round(value * 100))
#         if 0 <= value <= 100:
#             return int(round(value))
#         return None
#     if isinstance(value, str):
#         match = re.search(r"(\d{1,3})(?:\.\d+)?\s*%?", value)
#         if not match:
#             return None
#         num = int(match.group(1))
#         if 0 <= num <= 100:
#             return num
#     return None


# def _find_battery_in_payload(payload: Any) -> Optional[int]:
#     if isinstance(payload, dict):
#         for key, value in payload.items():
#             norm_key = key.lower().replace("_", "")
#             if "battery" in norm_key or norm_key in {"batt", "batterylevel", "batterypercent", "batterypct"}:
#                 pct = _coerce_battery_percent(value)
#                 if pct is not None:
#                     return pct
#             pct = _find_battery_in_payload(value)
#             if pct is not None:
#                 return pct
#         return None
#     if isinstance(payload, list):
#         for item in payload:
#             pct = _find_battery_in_payload(item)
#             if pct is not None:
#                 return pct
#         return None
#     return _coerce_battery_percent(payload)


# def _candidate_status_urls(phone_url: str, explicit_status_url: str) -> List[str]:
#     if explicit_status_url.strip():
#         return [explicit_status_url.strip()]

#     base = phone_url.split("?", 1)[0].rsplit("/", 1)[0]
#     return [
#         f"{base}/battery",
#         f"{base}/battery.json",
#         f"{base}/status.json",
#         f"{base}/status.html",
#         f"{base}/sensors.json",
#         f"{base}/status",
#         f"{base}/info.json",
#     ]


# def fetch_phone_battery(session: requests.Session, phone_url: str, status_url: str) -> Optional[int]:
#     for url in _candidate_status_urls(phone_url, status_url):
#         try:
#             resp = session.get(url, timeout=2)
#             if not resp.ok:
#                 continue

#             content_type = (resp.headers.get("Content-Type") or "").lower()
#             if "json" in content_type:
#                 pct = _find_battery_in_payload(resp.json())
#                 if pct is not None:
#                     return pct

#             pct = _find_battery_in_payload(resp.text)
#             if pct is not None:
#                 return pct
#         except Exception:
#             continue
#     return None


# # ── Main loop ─────────────────────────────────────────────────────────────────
# def main():
#     effective_demo = DEMO_MODE
#     model = None

#     print("=" * 62)
#     print("  ForestGuard AI — Android Camera Agent")
#     print(f"  Camera ID    : {CAMERA_ID}")
#     print(f"  Server URL   : {SERVER_URL}")
#     print(f"  Confidence   : ≥ {round(CONFIDENCE_MIN*100)}%")
#     if effective_demo:
#         print(f"  Mode         : DEMO (generates realistic fake detections)")
#     else:
#         print(f"  Phone URL    : {PHONE_URL}")
#         if PHONE_STATUS_URL.strip():
#             print(f"  Status URL   : {PHONE_STATUS_URL}")
#         print(f"  Mode         : REAL (IP Webcam + YOLOv8 inference)")
#     print("=" * 62)

#     db = init_buffer()
#     headers = {
#         "x-camera-secret": CAMERA_SECRET,
#         "Content-Type": "application/json",
#     }

#     if not effective_demo:
#         model = load_model()
#         if model is None:
#             print("[agent] No model available — switching to demo mode.")
#             effective_demo = True

#     session = requests.Session()
#     battery = max(0, min(100, BATTERY_FALLBACK))
#     battery_drain = 0.0

#     print(f"[agent] Starting. Posting every {POLL_INTERVAL}s...")
#     print(f"[agent] Press Ctrl+C to stop.\n")

#     while True:
#         try:
#             time.sleep(POLL_INTERVAL)
#             if effective_demo:
#                 battery_drain += 0.05
#                 current_battery = max(5, round(battery - battery_drain))
#                 battery_source = "simulated"
#             else:
#                 phone_battery = fetch_phone_battery(session, PHONE_URL, PHONE_STATUS_URL)
#                 if phone_battery is not None:
#                     battery = phone_battery
#                     battery_source = "phone"
#                 else:
#                     battery_source = "fallback"
#                 current_battery = battery

#             # Try to flush any buffered payloads first
#             flush_buffer(db, headers)

#             # ── Produce detections ──────────────────────────────────────────
#             if effective_demo:
#                 detections = demo_inference()
#             else:
#                 try:
#                     frame = grab_frame(session)
#                     h, w = frame.shape[:2]
#                     print(f"[agent] Frame grabbed ({w}x{h}) — running inference...")
#                     detections = run_inference(model, frame, w, h)
#                 except Exception as e:
#                     print(f"[agent] Phone unreachable: {e}")
#                     print(f"[agent]   Is IP Webcam running? Is the IP correct? ({PHONE_URL})")
#                     continue

#             # ── Build payload ───────────────────────────────────────────────
#             payload = {
#                 "camera_id":  CAMERA_ID,
#                 "detections": detections,
#                 "battery":    current_battery,
#                 "battery_source": battery_source,
#                 "timestamp":  datetime.now(timezone.utc).isoformat() + "Z",
#                 "source":     "simulated" if effective_demo else "real",
#             }

#             if not detections:
#                 print(f"[agent] Heartbeat only — no detections above threshold | battery: {current_battery}%")
#             else:
#                 species_list = ", ".join(
#                     f"{d['object']} ({round(d['confidence']*100)}%)" for d in detections
#                 )
#                 print(f"[agent] ✓ Detected: {species_list} | battery: {current_battery}%")

#             # ── POST to backend ─────────────────────────────────────────────
#             try:
#                 posted = False
#                 last_error = None
#                 for server_url in candidate_server_urls(SERVER_URL):
#                     try:
#                         r = requests.post(server_url, json=payload, headers=headers, timeout=5)
#                         if r.ok:
#                             resp = r.json()
#                             if resp.get("suppressed"):
#                                 print(f"[agent]   → Server suppressed: {resp['suppressed']}")
#                             else:
#                                 ev_id = resp.get("event", {}).get("id", "?") if isinstance(resp.get("event"), dict) else "?"
#                                 print(f"[agent]   → Accepted: event_id={ev_id}")
#                             posted = True
#                             break
#                         if r.status_code == 401:
#                             print(f"[agent]   → Auth failed. Check CAMERA_SECRET matches server.")
#                             posted = True
#                             break
#                         last_error = f"Server error {r.status_code}"
#                     except requests.exceptions.ConnectionError as e:
#                         last_error = str(e)
#                         continue
#                 if not posted:
#                     print(f"[agent]   → Cannot reach server ({last_error or SERVER_URL}), buffering")
#                     buffer_payload(db, payload)
#             except requests.exceptions.ConnectionError:
#                 print(f"[agent]   → Cannot reach server ({SERVER_URL}), buffering")
#                 buffer_payload(db, payload)
#             except Exception as e:
#                 print(f"[agent]   → Error posting: {e}, buffering")
#                 buffer_payload(db, payload)

#         except KeyboardInterrupt:
#             print("\n[agent] Stopped by user.")
#             break
#         except Exception as e:
#             print(f"[agent] Unexpected error: {e}")
#             time.sleep(2)


# if __name__ == "__main__":
#     main()
#!/usr/bin/env python3
"""
ForestGuard AI — Android Camera Agent (Fine-Tuned Model)
=========================================================
Uses YOUR fine-tuned best.pt model trained on 8 Nepal wildlife classes.
Only sends frames to dashboard when one of the 8 animals is detected.

QUICK START
-----------
Demo mode (no phone needed):
    DEMO_MODE=true CAMERA_ID=cam_01 python camera_agent.py

Real Android phone:
    PHONE_URL=http://192.168.1.69:8080/shot.jpg \
    CAMERA_ID=cam_02 \
    python camera_agent.py

ANDROID SETUP (IP Webcam)
--------------------------
1. Install "IP Webcam" from Play Store (by Pavel Khlebovich)
2. Open app → scroll to bottom → tap "Start server"
3. Note IP shown e.g. http://192.168.1.69:8080
4. Set PHONE_URL=http://192.168.1.69:8080/shot.jpg
5. Both phone and laptop must be on same WiFi
"""

import cv2
import requests
import time
import json
import random
import sqlite3
import os
import re
from datetime import datetime, timezone
from pathlib import Path
import numpy as np
from typing import Dict, List, Optional, Any

# ── CONFIG ────────────────────────────────────────────────
PHONE_URL        = os.getenv("PHONE_URL",    "http://192.168.1.69:8080/shot.jpg")
PHONE_STATUS_URL = os.getenv("PHONE_STATUS_URL", "")
SERVER_URL       = os.getenv("SERVER_URL",   "http://localhost:3000/api/camera-data")
CAMERA_ID        = os.getenv("CAMERA_ID",   "cam_01")
CAMERA_SECRET    = os.getenv("CAMERA_SECRET", "forestguard-dev-secret")
POLL_INTERVAL    = int(os.getenv("POLL_INTERVAL",  "4"))
CONFIDENCE_MIN   = float(os.getenv("CONFIDENCE_MIN", "0.40"))  # higher threshold
                                                                # for fine-tuned model
DEMO_MODE        = os.getenv("DEMO_MODE", "false").lower() in ("true", "1", "yes")
BATTERY_FALLBACK = int(os.getenv("BATTERY_FALLBACK", "85"))
YOLO_IMAGE_SIZE  = int(os.getenv("YOLO_IMAGE_SIZE", "640"))
BUFFER_DB        = Path("buffer.db")

# ── YOUR FINE-TUNED MODEL PATH ────────────────────────────
# Update this to wherever your best.pt is saved
MODEL_PATH = os.getenv(
    "MODEL_PATH",
    r"C:\Users\tejas\Desktop_Backup\runs\detect\ml\runs\nepal_wildlife_v1\weights\best.pt"
)

# ── YOUR 8 CUSTOM CLASSES ─────────────────────────────────
# Must match EXACTLY the order in your data.yaml names list
WILDLIFE_CLASSES = [
    "deer",       # class 0
    "elephant",   # class 1
    "leopard",    # class 2
    "macaque",    # class 3
    "peacock",    # class 4
    "rhino",      # class 5
    "tiger",      # class 6
    "wildboar",   # class 7
]

# Human detection — handled separately using pretrained YOLO
# because your fine-tuned model wasn't trained on humans
HUMAN_CLASS = "person"

# Demo animals (for demo mode)
DEMO_ANIMALS = WILDLIFE_CLASSES.copy()
# ─────────────────────────────────────────────────────────


def candidate_server_urls(primary_url: str) -> List[str]:
    urls = [primary_url]
    if "localhost" in primary_url:
        urls.append(primary_url.replace("localhost", "127.0.0.1"))
    elif "127.0.0.1" in primary_url:
        urls.append(primary_url.replace("127.0.0.1", "localhost"))
    if ":3000/" in primary_url:
        urls.append(primary_url.replace(":3000/", ":5173/"))
    deduped = []
    for url in urls:
        if url not in deduped:
            deduped.append(url)
    return deduped


# ── Offline buffer ────────────────────────────────────────
def init_buffer() -> sqlite3.Connection:
    db = sqlite3.connect(str(BUFFER_DB), check_same_thread=False)
    db.execute("""
        CREATE TABLE IF NOT EXISTS queue (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            payload    TEXT    NOT NULL,
            created_at REAL    NOT NULL
        )
    """)
    db.commit()
    return db


def buffer_payload(db: sqlite3.Connection, payload: Dict[str, Any]):
    db.execute(
        "INSERT INTO queue (payload, created_at) VALUES (?, ?)",
        (json.dumps(payload), time.time()),
    )
    db.commit()
    queued = db.execute("SELECT COUNT(*) FROM queue").fetchone()[0]
    print(f"  [buffer] Queued offline (total: {queued})")


def flush_buffer(db: sqlite3.Connection, headers: dict):
    rows = db.execute(
        "SELECT id, payload FROM queue ORDER BY id LIMIT 20"
    ).fetchall()
    flushed = 0
    for row_id, raw in rows:
        posted = False
        for server_url in candidate_server_urls(SERVER_URL):
            try:
                r = requests.post(
                    server_url,
                    json=json.loads(raw),
                    headers=headers,
                    timeout=5
                )
                if r.ok:
                    db.execute("DELETE FROM queue WHERE id = ?", (row_id,))
                    db.commit()
                    flushed += 1
                    posted = True
                    break
            except Exception:
                continue
        if not posted:
            break
    if flushed:
        print(f"  [buffer] Flushed {flushed} queued events")


# ── Load models ───────────────────────────────────────────
def load_models():
    """
    Loads two models:
      1. YOUR fine-tuned model (best.pt) — detects 8 Nepal wildlife
      2. Pretrained YOLOv8n — detects humans only
    Returns (wildlife_model, human_model)
    """
    from ultralytics import YOLO

    # Load your fine-tuned wildlife model
    if not Path(MODEL_PATH).exists():
        print(f"[agent] ERROR: best.pt not found at:")
        print(f"        {MODEL_PATH}")
        print(f"[agent] Update MODEL_PATH in camera_agent.py")
        print(f"[agent] Falling back to demo mode.")
        return None, None

    print(f"[agent] Loading fine-tuned wildlife model...")
    print(f"        {MODEL_PATH}")
    wildlife_model = YOLO(MODEL_PATH)
    print(f"[agent] ✅ Wildlife model loaded ({len(WILDLIFE_CLASSES)} classes)")

    # Load pretrained model for human detection only
    print(f"[agent] Loading pretrained model for human detection...")
    human_model = YOLO("yolov8n.pt")
    print(f"[agent] ✅ Human detection model loaded")

    return wildlife_model, human_model


# ── Core detection logic ──────────────────────────────────
def run_inference(
    wildlife_model,
    human_model,
    frame,
    frame_w: int,
    frame_h: int
) -> List[Dict[str, Any]]:
    """
    Runs two models on the frame:
      1. Fine-tuned model → detects 8 Nepal wildlife species
      2. Pretrained model → detects humans only

    KEY BEHAVIOR — Edge AI Gate:
      Only returns detections if at least one of the 8 animals
      OR a human is found. Empty frames are dropped completely.
      This is the "edge AI filtering" for your hackathon demo.
    """
    detections = []

    # ── 1. Wildlife detection (your fine-tuned model) ─────
    wildlife_results = wildlife_model.predict(
        frame,
        verbose=False,
        imgsz=YOLO_IMAGE_SIZE,
        conf=CONFIDENCE_MIN
    )[0]

    raw_count = len(wildlife_results.boxes)
    print(f"[yolo] Fine-tuned model raw detections: {raw_count}")

    for box in wildlife_results.boxes:
        cls_id   = int(box.cls[0])
        conf     = float(box.conf[0])
        cls_name = wildlife_model.names[cls_id]

        print(f"       {cls_name}: {round(conf*100)}%")

        # Only keep our 8 wildlife classes
        if cls_name not in WILDLIFE_CLASSES:
            continue
        if conf < CONFIDENCE_MIN:
            continue

        x1, y1, x2, y2 = box.xyxy[0].tolist()
        detections.append({
            "object":     cls_name,
            "confidence": round(conf, 3),
            "box": {
                "x": round(x1 / frame_w * 100, 2),
                "y": round(y1 / frame_h * 100, 2),
                "w": round((x2 - x1) / frame_w * 100, 2),
                "h": round((y2 - y1) / frame_h * 100, 2),
            },
        })

    # ── 2. Human detection (pretrained model) ─────────────
    human_results = human_model.predict(
        frame,
        verbose=False,
        imgsz=YOLO_IMAGE_SIZE,
        conf=0.50,             # higher threshold for humans
        classes=[0]            # class 0 = person in COCO
    )[0]

    for box in human_results.boxes:
        conf = float(box.conf[0])
        print(f"       person (human): {round(conf*100)}%")

        x1, y1, x2, y2 = box.xyxy[0].tolist()
        detections.append({
            "object":     "human",
            "confidence": round(conf, 3),
            "box": {
                "x": round(x1 / frame_w * 100, 2),
                "y": round(y1 / frame_h * 100, 2),
                "w": round((x2 - x1) / frame_w * 100, 2),
                "h": round((y2 - y1) / frame_h * 100, 2),
            },
        })

    # ── Edge AI gate ───────────────────────────────────────
    # If nothing detected → return empty → frame gets dropped
    # Dashboard only updates when something real is found
    if not detections:
        print(f"[yolo] No target species detected — frame dropped (edge AI gate)")

    return detections


# ── Demo inference ────────────────────────────────────────
def demo_inference() -> Optional[List[Dict[str, Any]]]:
    """Simulates realistic detections for demo without phone."""
    if random.random() < 0.50:   # 50% empty frames
        return None
    dets = []
    if random.random() < 0.15:   # 15% chance human detected
        dets.append({
            "object": "human",
            "confidence": round(random.uniform(0.65, 0.95), 2),
            "box": {
                "x": round(random.uniform(20, 50), 1),
                "y": round(random.uniform(10, 30), 1),
                "w": 14.0, "h": 42.0
            },
        })
    animal = random.choice(DEMO_ANIMALS)
    w = 36.0 if animal == "elephant" else 24.0
    h = 44.0 if animal == "elephant" else 30.0
    dets.append({
        "object": animal,
        "confidence": round(random.uniform(0.60, 0.99), 2),
        "box": {
            "x": round(random.uniform(5, 55), 1),
            "y": round(random.uniform(20, 50), 1),
            "w": w, "h": h
        },
    })
    return dets


# ── Grab frame from phone ─────────────────────────────────
def grab_frame(session: requests.Session):
    resp = session.get(
        f"{PHONE_URL}?t={int(time.time()*1000)}",
        timeout=4
    )
    resp.raise_for_status()
    img   = np.frombuffer(resp.content, np.uint8)
    frame = cv2.imdecode(img, cv2.IMREAD_COLOR)
    if frame is None:
        raise ValueError("Could not decode image from phone")
    return frame


# ── Battery helpers ───────────────────────────────────────
def _coerce_battery_percent(value: Any) -> Optional[int]:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        if 0 <= value <= 1:   return int(round(value * 100))
        if 0 <= value <= 100: return int(round(value))
        return None
    if isinstance(value, str):
        match = re.search(r"(\d{1,3})(?:\.\d+)?\s*%?", value)
        if not match: return None
        num = int(match.group(1))
        return num if 0 <= num <= 100 else None
    return None


def _find_battery_in_payload(payload: Any) -> Optional[int]:
    if isinstance(payload, dict):
        for key, value in payload.items():
            norm = key.lower().replace("_", "")
            if "battery" in norm or norm in {"batt", "batterylevel"}:
                pct = _coerce_battery_percent(value)
                if pct is not None: return pct
            pct = _find_battery_in_payload(value)
            if pct is not None: return pct
        return None
    if isinstance(payload, list):
        for item in payload:
            pct = _find_battery_in_payload(item)
            if pct is not None: return pct
        return None
    return _coerce_battery_percent(payload)


def fetch_phone_battery(
    session: requests.Session,
    phone_url: str,
    status_url: str
) -> Optional[int]:
    base = phone_url.split("?")[0].rsplit("/", 1)[0]
    candidates = (
        [status_url.strip()] if status_url.strip()
        else [f"{base}/battery", f"{base}/status.json", f"{base}/sensors.json"]
    )
    for url in candidates:
        try:
            resp = session.get(url, timeout=2)
            if not resp.ok: continue
            ct = (resp.headers.get("Content-Type") or "").lower()
            payload = resp.json() if "json" in ct else resp.text
            pct = _find_battery_in_payload(payload)
            if pct is not None: return pct
        except Exception:
            continue
    return None


# ── Main loop ─────────────────────────────────────────────
def main():
    effective_demo  = DEMO_MODE
    wildlife_model  = None
    human_model     = None

    print("=" * 62)
    print("  ForestGuard AI — Camera Agent (Fine-Tuned Model)")
    print(f"  Camera ID  : {CAMERA_ID}")
    print(f"  Server     : {SERVER_URL}")
    print(f"  Confidence : ≥ {round(CONFIDENCE_MIN*100)}%")
    print(f"  Classes    : {', '.join(WILDLIFE_CLASSES)} + human")
    if effective_demo:
        print(f"  Mode       : DEMO")
    else:
        print(f"  Phone      : {PHONE_URL}")
        print(f"  Model      : {MODEL_PATH}")
        print(f"  Mode       : REAL (IP Webcam + Fine-Tuned YOLO)")
    print("=" * 62)

    db      = init_buffer()
    headers = {
        "x-camera-secret": CAMERA_SECRET,
        "Content-Type":    "application/json",
    }

    if not effective_demo:
        try:
            from ultralytics import YOLO
            wildlife_model, human_model = load_models()
            if wildlife_model is None:
                print("[agent] Switching to demo mode.")
                effective_demo = True
        except ImportError:
            print("[agent] ultralytics not installed.")
            print("[agent] pip install ultralytics")
            print("[agent] Switching to demo mode.")
            effective_demo = True

    session         = requests.Session()
    battery         = max(0, min(100, BATTERY_FALLBACK))
    battery_drain   = 0.0

    print(f"\n[agent] Starting. Polling every {POLL_INTERVAL}s...")
    print(f"[agent] Press Ctrl+C to stop.\n")

    while True:
        try:
            time.sleep(POLL_INTERVAL)

            # ── Battery ───────────────────────────────────
            if effective_demo:
                battery_drain  += 0.05
                current_battery = max(5, round(battery - battery_drain))
                battery_source  = "simulated"
            else:
                phone_batt = fetch_phone_battery(
                    session, PHONE_URL, PHONE_STATUS_URL
                )
                if phone_batt is not None:
                    battery        = phone_batt
                    battery_source = "phone"
                else:
                    battery_source = "fallback"
                current_battery = battery

            flush_buffer(db, headers)

            # ── Detections ────────────────────────────────
            if effective_demo:
                detections = demo_inference()
            else:
                try:
                    frame     = grab_frame(session)
                    h, w      = frame.shape[:2]
                    print(f"[agent] Frame grabbed ({w}×{h}) — running inference...")
                    detections = run_inference(
                        wildlife_model, human_model, frame, w, h
                    )
                except Exception as e:
                    print(f"[agent] Phone error: {e}")
                    print(f"[agent]   Check IP Webcam is running ({PHONE_URL})")
                    continue

            # ── Edge AI gate ──────────────────────────────
            # Only POST if at least one animal or human detected
            # Empty frames are silently dropped — no dashboard update
            if not detections:
                print(
                    f"[agent] Frame dropped — no target species detected "
                    f"| battery: {current_battery}%"
                )
                continue   # ← KEY LINE: skips POST for empty frames

            # ── Build payload ─────────────────────────────
            payload = {
                "camera_id":      CAMERA_ID,
                "detections":     detections,
                "battery":        current_battery,
                "battery_source": battery_source,
                "timestamp":      datetime.now(timezone.utc).isoformat() + "Z",
                "source":         "simulated" if effective_demo else "real",
            }

            species_list = ", ".join(
                f"{d['object']} ({round(d['confidence']*100)}%)"
                for d in detections
            )
            print(f"[agent] ✅ Detected: {species_list} | battery: {current_battery}%")

            # ── POST to backend ───────────────────────────
            posted     = False
            last_error = None

            for server_url in candidate_server_urls(SERVER_URL):
                try:
                    r = requests.post(
                        server_url,
                        json=payload,
                        headers=headers,
                        timeout=5
                    )
                    if r.ok:
                        resp   = r.json()
                        ev_id  = (
                            resp.get("event", {}).get("id", "?")
                            if isinstance(resp.get("event"), dict) else "?"
                        )
                        print(f"[agent]   → Accepted: event_id={ev_id}")
                        posted = True
                        break
                    if r.status_code == 401:
                        print("[agent]   → Auth failed. Check CAMERA_SECRET.")
                        posted = True
                        break
                    last_error = f"HTTP {r.status_code}"
                except requests.exceptions.ConnectionError as e:
                    last_error = str(e)
                    continue

            if not posted:
                print(f"[agent]   → Cannot reach server ({last_error}), buffering")
                buffer_payload(db, payload)

        except KeyboardInterrupt:
            print("\n[agent] Stopped.")
            break
        except Exception as e:
            print(f"[agent] Unexpected error: {e}")
            time.sleep(2)


if __name__ == "__main__":
    main()