import os
import time
import uuid
import threading
from functools import wraps
from pathlib import Path
from flask import Flask, render_template, request, jsonify, session as flask_session
from flask_socketio import SocketIO, emit
from werkzeug.utils import secure_filename
from dotenv import load_dotenv
import db
import tryon

load_dotenv()

app = Flask(__name__)
app.secret_key = os.getenv("FLASK_SECRET_KEY", "dev-secret")

# Reject oversized uploads early (protects disk and memory).
app.config["MAX_CONTENT_LENGTH"] = int(os.getenv("MAX_UPLOAD_MB", "10")) * 1024 * 1024

# Staff PIN is enforced server-side. Never trust the client to gate admin actions.
STAFF_PIN = os.getenv("STAFF_PIN", "1234")

# Only these image types are accepted for uploads.
ALLOWED_EXTENSIONS = {"jpg", "jpeg", "png", "webp"}

# Try-on job durability: how many total attempts per item, and the pause between
# retries. A restart re-enqueues any item still short of its attempt budget.
TRYON_MAX_ATTEMPTS = int(os.getenv("TRYON_MAX_ATTEMPTS", "3"))
TRYON_RETRY_DELAY_SECONDS = int(os.getenv("TRYON_RETRY_DELAY_SECONDS", "10"))

# WebSocket origins. Lock this down in production via ALLOWED_ORIGINS (comma-separated).
_allowed_origins_env = os.getenv("ALLOWED_ORIGINS", "*")
_allowed_origins = (
    "*" if _allowed_origins_env.strip() == "*"
    else [o.strip() for o in _allowed_origins_env.split(",") if o.strip()]
)
socketio = SocketIO(app, cors_allowed_origins=_allowed_origins, async_mode="threading")

UPLOAD_DIR = Path("static/uploads")
SAREE_DIR = Path("static/sarees")
UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
SAREE_DIR.mkdir(parents=True, exist_ok=True)
RESULTS_DIR = Path("static/results")
RESULTS_DIR.mkdir(parents=True, exist_ok=True)

# The customer's own photograph is input data with no reason to outlive her visit.
# Generated results are kept only long enough for her to scan the QR and save them.
RESULT_RETENTION_HOURS = int(os.getenv("RESULT_RETENTION_HOURS", "24"))


def purge_customer_photo(session: dict):
    """Delete a session's customer photograph from disk and forget the reference."""
    filename = (session or {}).get("customer_photo")
    if not filename:
        return
    try:
        (UPLOAD_DIR / filename).unlink()
        print(f"[INFO] Deleted customer photo {filename}")
    except OSError as e:
        print(f"[WARNING] Could not delete customer photo {filename}: {e}")
    try:
        db.clear_customer_photo(session["id"])
    except Exception as e:
        print(f"[WARNING] Could not clear photo reference: {e}")


def purge_expired_results():
    """Remove generated try-on images older than the retention window."""
    cutoff = time.time() - RESULT_RETENTION_HOURS * 3600
    removed = 0
    for path in RESULTS_DIR.glob("*.png"):
        try:
            if path.stat().st_mtime < cutoff:
                path.unlink()
                removed += 1
        except OSError:
            pass
    if removed:
        print(f"[INFO] Purged {removed} result image(s) past the "
              f"{RESULT_RETENTION_HOURS}h retention window")
    return removed

db.init_db()


# ---------------------------------------------------------------------------
# Auth / upload helpers
# ---------------------------------------------------------------------------

def allowed_file(filename: str) -> bool:
    """True if the filename has a permitted image extension."""
    return "." in filename and filename.rsplit(".", 1)[1].lower() in ALLOWED_EXTENSIONS


def require_staff(view):
    """Gate a route behind server-side staff authentication."""
    @wraps(view)
    def wrapped(*args, **kwargs):
        if not flask_session.get("staff"):
            return jsonify({"error": "Staff authentication required"}), 401
        return view(*args, **kwargs)
    return wrapped


# ---------------------------------------------------------------------------
# Background try-on processing
# ---------------------------------------------------------------------------

# Cart items currently being worked on, so a duplicate add or a restart-recovery
# pass never spawns two workers for the same item.
_inflight_lock = threading.Lock()
_inflight: set[int] = set()


def process_tryon_async(cart_item_id: int, customer_photo_path: str, saree_image_path: str,
                        image_type: str | None = None, saree_name: str | None = None,
                        pleat_style: str | None = None):
    """Run the try-on with bounded retries and emit the result via SocketIO.

    Each attempt is recorded in the DB (status='processing', attempts++), so a
    crash mid-run leaves a durable trail that startup recovery can pick up.
    """
    last_error = None
    try:
        for attempt in range(1, TRYON_MAX_ATTEMPTS + 1):
            db.mark_processing(cart_item_id)
            try:
                result_path = tryon.run_tryon(
                    customer_photo_path, saree_image_path,
                    image_type=image_type, saree_name=saree_name,
                    pleat_style=pleat_style
                )
                db.set_result(cart_item_id, result_path, status="done")
                socketio.emit("result_ready", {
                    "cart_item_id": cart_item_id,
                    "result_url": "/" + result_path.replace("\\", "/"),
                    "status": "done"
                })
                print(f"[INFO] Try-on complete for cart_item {cart_item_id} (attempt {attempt})")
                return
            except tryon.PermanentTryonError as e:
                # Quota/billing/config failures won't succeed on retry — fail fast
                # so staff see an actionable message instead of a 60s spinner.
                last_error = str(e)
                print(f"[ERROR] Try-on permanently failed for cart_item {cart_item_id}: {e}")
                break
            except Exception as e:
                last_error = str(e)
                print(f"[ERROR] Try-on attempt {attempt}/{TRYON_MAX_ATTEMPTS} "
                      f"failed for cart_item {cart_item_id}: {e}")
                if attempt < TRYON_MAX_ATTEMPTS:
                    time.sleep(TRYON_RETRY_DELAY_SECONDS)

        # All attempts exhausted — mark as a durable error the UI can surface.
        db.set_result(cart_item_id, "", status="error", error_message=last_error)
        socketio.emit("result_ready", {
            "cart_item_id": cart_item_id,
            "result_url": None,
            "status": "error"
        })
    finally:
        with _inflight_lock:
            _inflight.discard(cart_item_id)


def start_tryon_thread(cart_item_id, customer_photo, saree_image,
                       image_type=None, saree_name=None, pleat_style=None):
    """Launch a daemon thread to process a try-on request (deduplicated)."""
    with _inflight_lock:
        if cart_item_id in _inflight:
            print(f"[INFO] cart_item {cart_item_id} already in flight; skipping duplicate worker")
            return
        _inflight.add(cart_item_id)

    t = threading.Thread(
        target=process_tryon_async,
        args=(cart_item_id, customer_photo, saree_image, image_type, saree_name,
              pleat_style),
        daemon=True
    )
    t.start()
    print(f"[INFO] Started try-on thread for cart_item {cart_item_id}")


def recover_incomplete_jobs():
    """Re-enqueue try-on jobs left unfinished by a previous crash/restart.

    Without this, an item stuck at 'pending'/'processing' when the process died
    would never complete — the kiosk would spin forever.
    """
    try:
        items = db.get_incomplete_cart_items(TRYON_MAX_ATTEMPTS)
    except Exception as e:
        print(f"[ERROR] Job recovery query failed: {e}")
        return

    if not items:
        return

    print(f"[INFO] Recovering {len(items)} incomplete try-on job(s) after restart")
    for it in items:
        if not it.get("customer_photo"):
            continue
        customer_photo_path = str(UPLOAD_DIR / it["customer_photo"])
        saree_image_path = str(SAREE_DIR / it["saree_filename"])
        start_tryon_thread(
            it["id"], customer_photo_path, saree_image_path,
            image_type=it.get("saree_image_type"), saree_name=it.get("saree_name"),
            pleat_style=it.get("pleat_style")
        )


# Recover any jobs interrupted by the previous run, on import (before serving).
recover_incomplete_jobs()
# Enforce the retention window on anything left behind by a previous run.
purge_expired_results()


# ---------------------------------------------------------------------------
# Admin Routes
# ---------------------------------------------------------------------------

@app.route("/admin/login", methods=["POST"])
def admin_login():
    """Authenticate staff via the server-side PIN and set a session cookie."""
    data = request.get_json(silent=True) or {}
    pin = str(data.get("pin", ""))
    if pin and pin == STAFF_PIN:
        flask_session["staff"] = True
        return jsonify({"ok": True})
    return jsonify({"error": "Invalid PIN"}), 401


@app.route("/admin/logout", methods=["POST"])
def admin_logout():
    """Clear staff authentication."""
    flask_session.pop("staff", None)
    return jsonify({"ok": True})


@app.route("/admin/auth-status")
def admin_auth_status():
    """Report whether the current client is authenticated as staff."""
    return jsonify({"authenticated": bool(flask_session.get("staff"))})


@app.route("/admin")
def admin_page():
    """Render the admin back-office page.

    Does NOT create a session — session creation is an explicit staff action,
    so merely opening (or refreshing) this page never disrupts a live customer.
    """
    active = db.get_active_session()
    session_id = active["id"] if active else None
    return render_template("admin.html", session_id=session_id)


@app.route("/admin/upload-saree", methods=["POST"])
@require_staff
def upload_saree():
    """Upload a new saree image to the catalog."""
    name = request.form.get("name", "").strip()
    code = request.form.get("code", "").strip()
    image_type = request.form.get("image_type", "fabric").strip().lower()
    if image_type not in ("fabric", "model"):
        image_type = "fabric"
    file = request.files.get("image")

    if not name or not code or not file:
        return jsonify({"error": "Name, code, and image are required"}), 400

    # secure_filename strips any path components (blocks ../ traversal); the
    # UUID prefix then prevents collisions between different uploads.
    original_filename = secure_filename(file.filename or "")
    if not original_filename or not allowed_file(original_filename):
        return jsonify({"error": "Unsupported image type. Use JPG, PNG, or WEBP."}), 400
    safe_filename = f"{uuid.uuid4().hex}_{original_filename}"
    file_path = SAREE_DIR / safe_filename
    file.save(file_path)

    saree_id = db.add_saree(name, code, safe_filename, image_type)

    return jsonify({
        "id": saree_id,
        "name": name,
        "code": code,
        "filename": safe_filename,
        "image_type": image_type
    })


@app.route("/admin/new-session", methods=["POST"])
@require_staff
def new_session():
    """Create a new customer session."""
    session_id = db.create_session()
    # Clear the big-screen selection so a new customer never inherits the
    # previous customer's try-on result.
    db.set_display_selection(None)
    socketio.emit("session_started", {"session_id": session_id})
    return jsonify({"session_id": session_id})


@app.route("/admin/end-session", methods=["POST"])
@require_staff
def end_session():
    """End the current active session."""
    active = db.get_active_session()
    if not active:
        return jsonify({"error": "No active session"}), 400

    db.end_session(active["id"])
    db.set_display_selection(None)
    # Privacy: her photograph goes the moment the session does.
    purge_customer_photo(active)
    purge_expired_results()
    socketio.emit("session_ended", {"session_id": active["id"]})
    return jsonify({"ok": True})


@app.route("/admin/delete-saree/<int:saree_id>", methods=["DELETE"])
@require_staff
def delete_saree(saree_id):
    """Delete a saree from the catalog and its file from disk."""
    filename = db.delete_saree(saree_id)
    if not filename:
        return jsonify({"error": "Saree not found"}), 404
    
    # Try to delete the file from disk
    file_path = SAREE_DIR / filename
    try:
        if file_path.exists():
            os.remove(file_path)
            print(f"[INFO] Deleted file from disk: {file_path}")
    except Exception as e:
        print(f"[WARNING] Could not delete saree file {file_path}: {e}")
        
    return jsonify({"ok": True})


# ---------------------------------------------------------------------------
# Kiosk Routes
# ---------------------------------------------------------------------------

@app.route("/kiosk")
def kiosk_page():
    """Render the customer kiosk page."""
    return render_template("kiosk.html")


@app.route("/kiosk/active-session")
def active_session_info():
    """Return info about the currently active session."""
    active = db.get_active_session()
    if not active:
        return jsonify({"active": False})
    return jsonify({
        "active": True,
        "id": active["id"],
        "customer_photo": active["customer_photo"],
        "photo_url": f"/static/uploads/{active['customer_photo']}" if active["customer_photo"] else None
    })


@app.route("/kiosk/sarees")
def get_sarees():
    """Return all sarees in the catalog as JSON."""
    sarees = db.get_all_sarees()
    for s in sarees:
        s["image_url"] = f"/static/sarees/{s['filename']}"
    return jsonify(sarees)


@app.route("/kiosk/cart")
def get_cart():
    """Return the cart for the active session."""
    active = db.get_active_session()
    if not active:
        return jsonify([])

    cart = db.get_cart(active["id"])
    # Add result_url field for convenience
    for item in cart:
        if item.get("result_path"):
            item["result_url"] = "/" + item["result_path"].replace("\\", "/")
        else:
            item["result_url"] = None
    return jsonify(cart)


@app.route("/kiosk/photo", methods=["POST"])
def upload_photo():
    """Upload a customer photo for the active session."""
    active = db.get_active_session()
    if not active:
        return jsonify({"error": "No active session"}), 400

    file = request.files.get("photo")
    if not file:
        return jsonify({"error": "No photo provided"}), 400

    # Validate type and strip any path components from the client-supplied name.
    original_name = secure_filename(file.filename or "photo.jpg")
    if not original_name or not allowed_file(original_name):
        return jsonify({"error": "Unsupported image type. Use JPG, PNG, or WEBP."}), 400

    # Save with session ID and UUID
    original_ext = Path(original_name).suffix or ".jpg"
    filename = f"{active['id']}_{uuid.uuid4().hex}{original_ext}"
    file_path = UPLOAD_DIR / filename
    file.save(file_path)

    db.set_customer_photo(active["id"], filename)

    return jsonify({
        "ok": True,
        "filename": filename,
        "photo_url": f"/static/uploads/{filename}"
    })


@app.route("/kiosk/cart/add", methods=["POST"])
def add_to_cart():
    """Add a saree to the active session's cart and start background try-on."""
    active = db.get_active_session()
    if not active:
        return jsonify({"error": "No active session"}), 400

    if not active.get("customer_photo"):
        return jsonify({"error": "Please upload a customer photo first"}), 400

    data = request.get_json()
    if not data or "saree_id" not in data:
        return jsonify({"error": "saree_id is required"}), 400

    saree_id = data["saree_id"]
    pleat_style = str(data.get("pleat_style", "single")).strip().lower()
    if pleat_style not in ("single", "double"):
        pleat_style = "single"

    try:
        cart_item_id = db.add_to_cart(active["id"], saree_id, pleat_style)
    except ValueError as e:
        return jsonify({"error": str(e)}), 400

    # Get saree info for the response
    cart_item = db.get_cart_item(cart_item_id)

    # Start background try-on processing
    customer_photo_path = str(UPLOAD_DIR / active["customer_photo"])
    saree_image_path = str(SAREE_DIR / cart_item["saree_filename"])
    start_tryon_thread(
        cart_item_id, customer_photo_path, saree_image_path,
        image_type=cart_item.get("saree_image_type"),
        saree_name=cart_item.get("saree_name"),
        pleat_style=cart_item.get("pleat_style")
    )

    return jsonify({
        "cart_item_id": cart_item_id,
        "saree_id": saree_id,
        "saree_name": cart_item["saree_name"],
        "saree_code": cart_item["saree_code"],
        "saree_filename": cart_item["saree_filename"],
        "pleat_style": pleat_style,
        "status": "pending"
    })


@app.route("/kiosk/cart/select", methods=["POST"])
def select_cart_item():
    """Select a cart item and broadcast to display clients."""
    data = request.get_json()
    if not data or "cart_item_id" not in data:
        return jsonify({"error": "cart_item_id is required"}), 400

    item = db.get_cart_item(data["cart_item_id"])
    if not item:
        return jsonify({"error": "Cart item not found"}), 404

    # Broadcast to all display clients
    result_url = None
    if item.get("result_path"):
        result_url = "/" + item["result_path"].replace("\\", "/")

    db.set_display_selection(item["id"])

    socketio.emit("show_saree", {
        "cart_item_id": item["id"],
        "saree_name": item["saree_name"],
        "saree_code": item["saree_code"],
        "pleat_style": item.get("pleat_style"),
        "status": item["status"],
        "result_url": result_url
    })

    return jsonify({
        "status": item["status"],
        "result_url": result_url
    })


# ---------------------------------------------------------------------------
# Display Route
# ---------------------------------------------------------------------------

@app.route("/display")
def display_page():
    """Render the fullscreen display page."""
    return render_template("display.html")


@app.route("/display/current")
def display_current():
    """Return the cart item currently selected for the display screen."""
    selected_id = db.get_display_selection()
    if not selected_id:
        return jsonify({"selected": False})

    item = db.get_cart_item(selected_id)
    if not item:
        return jsonify({"selected": False})

    result_url = None
    if item.get("result_path"):
        result_url = "/" + item["result_path"].replace("\\", "/")

    return jsonify({
        "selected": True,
        "cart_item_id": item["id"],
        "saree_name": item["saree_name"],
        "saree_code": item["saree_code"],
        "pleat_style": item.get("pleat_style"),
        "status": item["status"],
        "result_url": result_url
    })


# ---------------------------------------------------------------------------
# Status Polling Endpoint
# ---------------------------------------------------------------------------

@app.route("/result-status/<int:cart_item_id>")
def result_status(cart_item_id):
    """Return the processing status of a cart item (polling fallback)."""
    item = db.get_cart_item(cart_item_id)
    if not item:
        return jsonify({"error": "Cart item not found"}), 404

    result_url = None
    if item.get("result_path"):
        result_url = "/" + item["result_path"].replace("\\", "/")

    return jsonify({
        "status": item["status"],
        "result_url": result_url,
        "attempts": item.get("attempts"),
        "error_message": item.get("error_message")
    })


# ---------------------------------------------------------------------------
# SocketIO Events
# ---------------------------------------------------------------------------

@socketio.on("connect")
def handle_connect():
    """Handle client connection."""
    print("[INFO] Client connected")


@socketio.on("disconnect")
def handle_disconnect():
    """Handle client disconnection."""
    print("[INFO] Client disconnected")


@socketio.on("select_saree")
def handle_select_saree(data):
    """Handle saree selection from kiosk — broadcast to display."""
    cart_item_id = data.get("cart_item_id")
    if not cart_item_id:
        return

    item = db.get_cart_item(cart_item_id)
    if not item:
        return

    result_url = None
    if item.get("result_path"):
        result_url = "/" + item["result_path"].replace("\\", "/")

    db.set_display_selection(item["id"])

    emit("show_saree", {
        "cart_item_id": item["id"],
        "saree_name": item["saree_name"],
        "saree_code": item["saree_code"],
        "pleat_style": item.get("pleat_style"),
        "status": item["status"],
        "result_url": result_url
    }, broadcast=True)


# ---------------------------------------------------------------------------
# Entry Point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    # debug=True exposes the Werkzeug debugger (arbitrary code execution) and
    # must stay off unless explicitly enabled for local development.
    debug_mode = os.getenv("FLASK_DEBUG", "false").lower() == "true"
    print("=" * 60)
    print("  Saree Virtual Try-On Server")
    print("  ----------------------------")
    print(f"  Backend: {tryon.BACKEND}")
    print(f"  Debug:   {debug_mode}")
    print(f"  Admin:   http://localhost:5001/admin")
    print(f"  Kiosk:   http://localhost:5001/kiosk")
    print(f"  Display: http://localhost:5001/display")
    print("=" * 60)
    socketio.run(app, host="0.0.0.0", port=5001, debug=debug_mode, allow_unsafe_werkzeug=True)
