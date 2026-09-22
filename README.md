# Try-On — Saree Virtual Try-On Smart Mirror

Photograph a customer **once**, then let her see herself in every saree in the
shop — including the ones that exist only as folded cloth — on a full-height
display, with her choice of drape.

Buying a saree today means physically draping fifteen or twenty. It exhausts the
customer, ties up a salesperson for an afternoon, and wears out the stock. Try-On
replaces that with one photograph and a screen.

---

## What it does

- **One photo, every saree.** A single full-body photograph is reused for the
  whole catalogue.
- **Works from fabric alone.** Most sarees in a shop have no model-worn photo —
  they sit folded on a shelf. Try-On generates the drape from a flat photograph
  of the cloth.
- **She chooses the drape.** Single-pleat or double-pleat pallu, selected per
  saree and recorded with the result.
- **Live big-screen display.** Results appear on a portrait screen the moment
  they are ready; the customer switches between sarees from a tablet.
- **Compare two looks** side by side without re-draping either.
- **Take it home.** A QR code hands the whole session to her phone.
- **Privacy by default.** The customer's photograph is deleted the moment the
  session ends; generated results expire automatically.

---

## Architecture

Three browser interfaces served by one Flask app, kept in sync over WebSockets:

| Interface | Route | Who uses it |
|-----------|-------|-------------|
| **Admin** | `/admin` | Staff — start/end sessions, upload sarees, manage the catalogue (PIN-gated) |
| **Kiosk** | `/kiosk` | Customer/staff — photo capture, catalogue, cart, drape selection |
| **Display** | `/display` | The big screen — one saree at a time, updating live |

Try-on generation runs in a background worker with bounded retries and
crash-recovery, so an interrupted job resumes on restart.

### Try-on backends

Generation is routed per saree via `TRYON_BACKEND=auto`:

- **Model-worn photos** → a warp-based virtual try-on model.
- **Fabric / flat-lay photos** → a generative image-edit model that constructs
  the drape from the cloth and the customer photo.

Backends are swappable by configuration; see `.env.example`.

---

## Tech stack

- **Backend:** Python, Flask, Flask-SocketIO
- **Storage:** SQLite
- **Frontend:** server-rendered HTML/CSS/JS (no build step)
- **Image processing:** Pillow

---

## Getting started

```bash
# 1. Create and activate a virtual environment
python3 -m venv .venv
source .venv/bin/activate

# 2. Install dependencies
pip install -r requirements.txt

# 3. Configure
cp .env.example .env
#    then edit .env — set the try-on backend and any API keys

# 4. Run
python run_server.py
```

Then open:

- Admin — <http://localhost:5001/admin>
- Kiosk — <http://localhost:5001/kiosk>
- Display — <http://localhost:5001/display> (press **F11** for fullscreen)

On the same Wi-Fi, other devices reach the kiosk at
`http://<this-machine-ip>:5001/kiosk`.

---

## Project layout

```
app.py            Flask routes, session lifecycle, background worker
tryon.py          Try-on backends and per-saree routing
db.py             SQLite schema and queries
run_server.py     Entry point
templates/        admin.html · kiosk.html · display.html
static/           style.css, saree catalogue images
```

---

## Status

Working, integrated system validated end to end — **Technology Readiness
Level 4**. Next step is deployment on dedicated kiosk hardware in a working
retail shop (TRL 5–6).

---

## Privacy

Customer photographs are treated as personal data under India's Digital Personal
Data Protection Act, 2023. They are deleted from disk when the session ends, and
generated results are purged automatically after a retention window. No face
recognition, profiling, or sharing of any kind.
