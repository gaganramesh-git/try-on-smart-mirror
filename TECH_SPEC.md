# Saree Virtual Try-On — Technical Specification

> **For the AI building this:** Read this entire file before writing a single line of code.
> All architectural decisions are made. Your job is to implement exactly what is described here.
> Do not introduce frameworks, ORMs, build tools, or abstractions not listed in this document.
> When in doubt, do the simpler thing.

---

## Stack at a Glance

Current implementation note: the production try-on backend is
`Kwai-Kolors/Kolors-Virtual-Try-On` on Hugging Face Spaces. Older IDM-VTON
references in historical notes are obsolete and should not be used for new
generation work.

| Layer | Technology | Why |
|---|---|---|
| Backend | Python 3.10+, Flask | Simple, student-maintainable, minimal boilerplate |
| Real-time sync | Flask-SocketIO + eventlet | WebSocket between kiosk and display pages |
| Database | SQLite via Python `sqlite3` stdlib | Zero setup, single file, enough for this scale |
| Frontend | Vanilla HTML + CSS + JavaScript | No build step, no framework, works immediately |
| Try-on AI | HuggingFace Spaces — IDM-VTON (free) | Best free try-on model; called via `gradio_client` |
| Fallback AI | Google Gemini 2.0 Flash (free tier) | If HuggingFace queue is too long during demo |
| Image storage | Local filesystem (`/static/uploads/`, `/static/results/`) | No S3, no cloud, no complexity |
| Background jobs | Python `threading.Thread` | Simple enough; no Celery, no Redis, no queues |
| Environment vars | `python-dotenv` + `.env` file | API keys stay out of source code |

---

## Project File Structure

Build this exact structure. Do not add files or folders not listed here.

```
saree-tryon/
│
├── app.py                   # Flask app, all routes, SocketIO events
├── db.py                    # Database init and all query functions
├── tryon.py                 # All try-on API logic (HuggingFace + Gemini fallback)
├── .env                     # API keys — never commit this
├── .env.example             # Template for .env — commit this
├── requirements.txt         # Exact pinned dependencies
│
├── static/
│   ├── uploads/             # Customer photos (written by app)
│   │   └── .gitkeep
│   ├── results/             # Try-on output images (written by app)
│   │   └── .gitkeep
│   ├── sarees/              # Saree catalog images (written by admin upload)
│   │   └── .gitkeep
│   └── style.css            # One shared CSS file for all pages
│
└── templates/
    ├── kiosk.html           # Customer-facing: browse catalog, build cart
    ├── display.html         # Big screen: fullscreen try-on result
    └── admin.html           # Staff: upload sarees, start/end sessions
```

---

## Dependencies

### `requirements.txt` — use these exact packages

```
flask==3.0.3
flask-socketio==5.3.6
eventlet==0.36.1
gradio_client==0.17.0
google-generativeai==0.7.2
python-dotenv==1.0.1
Pillow==10.3.0
```

Install with: `pip install -r requirements.txt`

---

## Environment Variables

Current Kolors configuration:

```
TRYON_BACKEND=kolors
KOLORS_SPACE_URL=https://kwai-kolors-kolors-virtual-try-on.hf.space
KOLORS_SEED=42
KOLORS_RANDOMIZE_SEED=false
```

### `.env.example`

```
# Get this free at: aistudio.google.com
GEMINI_API_KEY=your_gemini_api_key_here

# Set to "huggingface" or "gemini"
TRYON_BACKEND=huggingface

# Flask
FLASK_SECRET_KEY=any-random-string-change-this
```

### `.env` (developer fills this in — never committed)

```
GEMINI_API_KEY=AIza...actualkey...
TRYON_BACKEND=huggingface
FLASK_SECRET_KEY=saree-demo-secret-2024
```

---

## Database Schema

File: `db.py`

Use raw `sqlite3`. No ORM. Database file: `saree_tryon.db` in the project root.

```python
import sqlite3
import os

DB_PATH = "saree_tryon.db"

def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    conn = get_db()
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS sarees (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            name        TEXT NOT NULL,
            code        TEXT NOT NULL,
            filename    TEXT NOT NULL,
            created_at  DATETIME DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS sessions (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            customer_photo  TEXT,
            status      TEXT DEFAULT 'active',
            created_at  DATETIME DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS cart_items (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id  INTEGER NOT NULL,
            saree_id    INTEGER NOT NULL,
            result_path TEXT,
            status      TEXT DEFAULT 'pending',
            FOREIGN KEY (session_id) REFERENCES sessions(id),
            FOREIGN KEY (saree_id)   REFERENCES sarees(id)
        );
    """)
    conn.commit()
    conn.close()
```

### Query functions to implement in `db.py`

```python
def create_session() -> int:
    # inserts a new session, returns session id

def set_customer_photo(session_id: int, filename: str):
    # updates sessions.customer_photo

def get_active_session() -> dict | None:
    # returns the most recent active session row

def end_session(session_id: int):
    # sets status = 'ended'

def get_all_sarees() -> list[dict]:
    # returns all rows from sarees table

def add_saree(name: str, code: str, filename: str) -> int:
    # inserts a saree, returns new id

def add_to_cart(session_id: int, saree_id: int) -> int:
    # inserts a cart_item with status='pending', returns cart_item id
    # enforce max 20 items per session — raise ValueError if exceeded

def get_cart(session_id: int) -> list[dict]:
    # returns all cart_items joined with saree data for given session

def set_result(cart_item_id: int, result_path: str):
    # updates result_path and status='done' for a cart item

def get_cart_item(cart_item_id: int) -> dict:
    # returns single cart_item row
```

---

## Try-On Module

File: `tryon.py`

This module contains all AI integration. `app.py` imports and calls only `run_tryon()`.

```python
import os
import uuid
from pathlib import Path
from dotenv import load_dotenv

load_dotenv()

RESULTS_DIR = Path("static/results")
RESULTS_DIR.mkdir(parents=True, exist_ok=True)

BACKEND = os.getenv("TRYON_BACKEND", "huggingface")


def run_tryon(customer_photo_path: str, saree_image_path: str) -> str:
    """
    Main entry point. Returns the local file path of the result image.
    Raises RuntimeError on failure.
    """
    if BACKEND == "huggingface":
        return _tryon_huggingface(customer_photo_path, saree_image_path)
    elif BACKEND == "gemini":
        return _tryon_gemini(customer_photo_path, saree_image_path)
    else:
        raise ValueError(f"Unknown TRYON_BACKEND: {BACKEND}")


def _tryon_huggingface(customer_photo_path: str, saree_image_path: str) -> str:
    """
    Calls IDM-VTON on HuggingFace Spaces (free).
    Model space: yisol/IDM-VTON
    Returns local path to saved result image.
    """
    from gradio_client import Client, handle_file

    client = Client("yisol/IDM-VTON")

    result = client.predict(
        garm_img=handle_file(saree_image_path),
        human_img=handle_file(customer_photo_path),
        garment_des="a beautiful Indian saree",
        is_checked=True,
        is_checked_crop=False,
        denoise_steps=30,
        seed=42,
        api_name="/tryon"
    )

    # result[0] is the path to the generated image (temp file)
    source_path = result[0]

    output_filename = f"{uuid.uuid4().hex}.png"
    output_path = RESULTS_DIR / output_filename

    import shutil
    shutil.copy(source_path, output_path)

    return str(output_path)


def _tryon_gemini(customer_photo_path: str, saree_image_path: str) -> str:
    """
    Fallback: uses Gemini 2.0 Flash image generation.
    Lower identity preservation than IDM-VTON — use only if HuggingFace is down.
    Free tier: 15 requests/min, 1500/day.
    Returns local path to saved result image.
    """
    import google.generativeai as genai
    from PIL import Image
    import io

    genai.configure(api_key=os.getenv("GEMINI_API_KEY"))
    model = genai.GenerativeModel("gemini-2.0-flash-exp")

    customer_img = Image.open(customer_photo_path)
    saree_img = Image.open(saree_image_path)

    prompt = (
        "You are given two images. The first is a full-body photo of a woman. "
        "The second is a saree. Generate a photorealistic image of the same woman "
        "wearing this exact saree. Preserve her face, height, skin tone, and body "
        "proportions exactly. Only the clothing should change."
    )

    response = model.generate_content([prompt, customer_img, saree_img])

    # Extract image bytes from response
    for part in response.candidates[0].content.parts:
        if part.inline_data and "image" in part.inline_data.mime_type:
            image_bytes = part.inline_data.data
            output_filename = f"{uuid.uuid4().hex}.png"
            output_path = RESULTS_DIR / output_filename
            with open(output_path, "wb") as f:
                f.write(image_bytes)
            return str(output_path)

    raise RuntimeError("Gemini returned no image in response")
```

---

## Flask Application

File: `app.py`

### Imports and setup

```python
import os
import uuid
import threading
from pathlib import Path
from flask import Flask, render_template, request, jsonify, session
from flask_socketio import SocketIO, emit
from dotenv import load_dotenv
import db
import tryon

load_dotenv()

app = Flask(__name__)
app.secret_key = os.getenv("FLASK_SECRET_KEY", "dev-secret")
socketio = SocketIO(app, cors_allowed_origins="*", async_mode="eventlet")

UPLOAD_DIR = Path("static/uploads")
SAREE_DIR  = Path("static/sarees")
UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
SAREE_DIR.mkdir(parents=True, exist_ok=True)

db.init_db()
```

### Routes to implement

#### Admin routes

```
GET  /admin                → render admin.html
POST /admin/upload-saree   → save saree image, insert into db, return JSON {id, name, code, filename}
POST /admin/new-session    → create new session, return JSON {session_id}
POST /admin/end-session    → end active session, return JSON {ok: true}
```

#### Kiosk routes

```
GET  /kiosk                → render kiosk.html
GET  /kiosk/sarees         → return JSON list of all sarees [{id, name, code, filename}]
GET  /kiosk/cart           → return JSON cart for active session
POST /kiosk/photo          → receive uploaded photo, save to static/uploads/, update session
POST /kiosk/cart/add       → body: {saree_id}
                             add to cart, fire background thread for try-on, return {cart_item_id}
POST /kiosk/cart/select    → body: {cart_item_id}
                             emit SocketIO event "show_saree" to display clients
                             return {status, result_url or null}
```

#### Display routes

```
GET  /display              → render display.html
```

#### Static helper

```
GET  /result-status/<cart_item_id>  → return {status: "pending"|"done", result_url: "/static/results/..."}
```

### Background try-on thread

Call this from the `POST /kiosk/cart/add` route after inserting the cart item:

```python
def process_tryon_async(cart_item_id: int, customer_photo_path: str, saree_image_path: str):
    try:
        result_path = tryon.run_tryon(customer_photo_path, saree_image_path)
        db.set_result(cart_item_id, result_path)
        # Notify all connected clients that this result is ready
        socketio.emit("result_ready", {
            "cart_item_id": cart_item_id,
            "result_url": "/" + result_path.replace("\\", "/")
        })
    except Exception as e:
        print(f"[ERROR] try-on failed for cart_item {cart_item_id}: {e}")
        # Set status to 'error' in db so the UI can show a retry button

def start_tryon_thread(cart_item_id, customer_photo, saree_image):
    t = threading.Thread(
        target=process_tryon_async,
        args=(cart_item_id, customer_photo, saree_image),
        daemon=True
    )
    t.start()
```

### SocketIO events

```python
@socketio.on("connect")
def handle_connect():
    pass  # nothing needed — connection tracked automatically

@socketio.on("select_saree")
def handle_select_saree(data):
    # data = {"cart_item_id": int}
    # look up result from db
    item = db.get_cart_item(data["cart_item_id"])
    emit("show_saree", {
        "cart_item_id": item["id"],
        "saree_name": item["saree_name"],   # join this in db.get_cart_item
        "saree_code": item["saree_code"],
        "status": item["status"],
        "result_url": "/" + item["result_path"] if item["result_path"] else None
    }, broadcast=True)
```

---

## Frontend Pages

### Shared `static/style.css`

Write clean, minimal CSS. Key rules:

```css
* { box-sizing: border-box; margin: 0; padding: 0; }
body { font-family: system-ui, sans-serif; background: #f5f5f5; color: #222; }

/* Kiosk */
.catalog-grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(120px, 1fr)); gap: 12px; padding: 16px; }
.saree-card { border: 2px solid transparent; border-radius: 8px; overflow: hidden; cursor: pointer; background: white; }
.saree-card.in-cart { border-color: #4A90E2; }
.saree-card img { width: 100%; aspect-ratio: 3/4; object-fit: cover; }
.saree-card .label { font-size: 11px; padding: 4px 6px; text-align: center; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }

.cart-strip { position: fixed; bottom: 0; left: 0; right: 0; background: white; border-top: 1px solid #ddd; padding: 8px 12px; display: flex; gap: 8px; overflow-x: auto; min-height: 72px; align-items: center; }
.cart-thumb { width: 44px; height: 58px; border-radius: 4px; object-fit: cover; cursor: pointer; border: 2px solid transparent; flex-shrink: 0; }
.cart-thumb.selected { border-color: #E85D24; }

/* Display */
body.display-page { background: #000; display: flex; align-items: center; justify-content: center; height: 100vh; }
#display-image { max-height: 100vh; max-width: 100vw; object-fit: contain; }
#display-label { position: fixed; bottom: 24px; left: 50%; transform: translateX(-50%); color: rgba(255,255,255,0.7); font-size: 16px; text-align: center; }
#loading-spinner { color: white; font-size: 18px; }

/* Admin */
.admin-container { max-width: 600px; margin: 40px auto; padding: 0 20px; }
.upload-form { background: white; padding: 20px; border-radius: 8px; margin-bottom: 20px; }
```

---

### `templates/kiosk.html`

Structure only — implement fully:

```html
<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Saree Try-On — Kiosk</title>
  <link rel="stylesheet" href="/static/style.css">
  <script src="https://cdn.socket.io/4.7.5/socket.io.min.js"></script>
</head>
<body>
  <!-- TOP BAR: session info + photo capture button -->
  <div id="topbar">
    <button id="btn-photo">Take Photo</button>
    <span id="session-status">No active session</span>
    <span id="cart-count">Cart: 0 / 20</span>
  </div>

  <!-- PHOTO CAPTURE (hidden by default, shown on button click) -->
  <div id="photo-section" style="display:none">
    <video id="webcam" autoplay></video>
    <button id="btn-capture">Capture</button>
    <!-- OR: file upload fallback -->
    <input type="file" id="file-upload" accept="image/*">
  </div>

  <!-- CATALOG GRID -->
  <div id="catalog" class="catalog-grid">
    <!-- saree cards injected by JS -->
  </div>

  <!-- CART STRIP (fixed bottom) -->
  <div class="cart-strip" id="cart-strip">
    <span id="cart-empty-msg">Add sarees to your cart</span>
    <!-- cart thumbnails injected by JS -->
  </div>
</body>
</html>
```

**JavaScript behaviour for `kiosk.html`:**

```javascript
// On load:
// 1. GET /kiosk/sarees → render catalog cards
// 2. GET /kiosk/cart   → render cart strip, mark in-cart sarees

// On saree card click:
// - if not in cart and cart < 20: POST /kiosk/cart/add {saree_id}
//   → add thumb to cart strip, mark card as in-cart
// - if already in cart: scroll cart strip to that thumb

// On cart thumb click:
// - emit socket event "select_saree" {cart_item_id}
// - mark this thumb as .selected, remove from others
// - if status is "done": display is updated by server broadcast
// - if status is "pending": show "Processing..." on display (server sends result_ready when done)

// On socket event "result_ready":
// - if cart_item_id matches currently selected: update selected thumb indicator
// - the display page handles showing the image automatically

// Photo capture:
// - getUserMedia → show video preview
// - on capture: canvas.toBlob → POST /kiosk/photo as multipart
// - fallback: file input → same POST /kiosk/photo

// Poll /result-status/<id> every 3 seconds for pending items as a fallback
// if WebSocket misses the result_ready event
```

---

### `templates/display.html`

```html
<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <title>Saree Try-On — Display</title>
  <link rel="stylesheet" href="/static/style.css">
  <script src="https://cdn.socket.io/4.7.5/socket.io.min.js"></script>
</head>
<body class="display-page">

  <div id="loading-spinner" style="display:none">Processing...</div>

  <img id="display-image" style="display:none" alt="Try-on result">

  <div id="display-label"></div>

  <script>
    const socket = io();
    const img = document.getElementById("display-image");
    const spinner = document.getElementById("loading-spinner");
    const label = document.getElementById("display-label");

    socket.on("show_saree", (data) => {
      if (data.status === "done" && data.result_url) {
        img.src = data.result_url + "?t=" + Date.now(); // cache-bust
        img.style.display = "block";
        spinner.style.display = "none";
        label.textContent = data.saree_name + " — " + data.saree_code;
      } else {
        img.style.display = "none";
        spinner.style.display = "block";
        label.textContent = "Processing " + data.saree_name + "...";
      }
    });

    socket.on("result_ready", (data) => {
      // If the currently shown item just finished processing
      if (img.style.display === "none") {
        img.src = data.result_url + "?t=" + Date.now();
        img.style.display = "block";
        spinner.style.display = "none";
      }
    });
  </script>
</body>
</html>
```

---

### `templates/admin.html`

Implement as a simple HTML form page. Required elements:

1. **Upload saree form** — fields: saree name (text), saree code (text), image file (file input). POST to `/admin/upload-saree`. Show uploaded sarees in a list below.
2. **New session button** — POST `/admin/new-session`. Shows session ID in the UI.
3. **End session button** — POST `/admin/end-session`.
4. No JavaScript framework needed — plain form submissions or simple fetch() calls are fine.

---

## How to Run

```bash
# 1. Clone / create project folder
cd saree-tryon

# 2. Create virtual environment
python -m venv venv
source venv/bin/activate      # Windows: venv\Scripts\activate

# 3. Install dependencies
pip install -r requirements.txt

# 4. Set up environment variables
cp .env.example .env
# Edit .env — add your GEMINI_API_KEY if using Gemini backend

# 5. Run the server
python app.py

# 6. Open two browser windows:
#    Window 1: http://localhost:5000/admin   → create session, upload sarees
#    Window 2: http://localhost:5000/kiosk   → customer view
#    Window 3: http://localhost:5000/display → big screen view (press F11)
```

`app.py` must run the server on port 5000 with SocketIO:

```python
if __name__ == "__main__":
    socketio.run(app, host="0.0.0.0", port=5000, debug=True)
```

---

## Important Implementation Notes

### Image requirements for IDM-VTON

- **Customer photo**: full-body shot, person centred, plain background preferred. Minimum 512×512px. Resize and pad to square (768×1024) before sending to API using Pillow.
- **Saree image**: flat-lay or mannequin. Crop to just the garment, no people. Minimum 512×512px.
- Do this resizing in `tryon.py` before making the API call.

### HuggingFace Spaces rate limits

- The free space at `yisol/IDM-VTON` may have a queue during peak hours
- The `gradio_client` call is blocking — run it only inside a `threading.Thread`
- Never call `tryon.run_tryon()` from inside a Flask route directly — always thread it
- If HuggingFace queue wait exceeds 60s, log a warning and optionally switch to Gemini fallback

### WebSocket reliability

- The kiosk page must reconnect automatically if the WebSocket drops (Socket.IO client does this by default)
- The display page must also auto-reconnect — Socket.IO handles this
- On reconnection, the kiosk should re-fetch `/kiosk/cart` and re-render the current state

### File naming

- Customer photos: `static/uploads/{session_id}_{uuid}.jpg`
- Saree images: `static/sarees/{uuid}_{original_filename}`
- Result images: `static/results/{uuid}.png`
- Never use user-supplied filenames directly — always prefix with a UUID

### Error handling rules

- If IDM-VTON fails: set `cart_item.status = 'error'` in db, emit `result_ready` with `{status: 'error'}` to clients
- If a saree image is missing: return 404 from the route, handle gracefully in frontend
- If no active session exists when kiosk tries to add to cart: return `{error: "No active session"}` with HTTP 400
- All errors must be logged with `print(f"[ERROR] ...")` — no silent failures

---

## Pre-Demo Checklist

Before the demo, run this to pre-generate all results so there is no live API wait:

```python
# pre_generate.py — run this the night before the demo
# Usage: python pre_generate.py <customer_photo_path>
import sys
import db
import tryon

db.init_db()
customer_photo = sys.argv[1]

sarees = db.get_all_sarees()
print(f"Generating try-ons for {len(sarees)} sarees...")

for s in sarees:
    saree_path = f"static/sarees/{s['filename']}"
    try:
        result = tryon.run_tryon(customer_photo, saree_path)
        print(f"Done: {s['name']} → {result}")
    except Exception as e:
        print(f"Failed: {s['name']} — {e}")
```

During the demo, results will load instantly from disk instead of waiting for the API.

---

## Security Notes (minimal, for local demo)

- The app runs on `localhost` only — not exposed to the internet
- No authentication needed for the demo
- In production: add a PIN code to the admin page at minimum
- Never commit `.env` — it contains API keys
