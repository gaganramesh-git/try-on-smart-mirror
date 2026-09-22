import sqlite3

DB_PATH = "saree_tryon.db"


def get_db():
    """Get a database connection with Row factory for dict-like access."""
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def _column_exists(conn, table: str, column: str) -> bool:
    """True if the given column already exists on the table."""
    cols = [r[1] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()]
    return column in cols


def _migrate(conn):
    """Apply idempotent schema migrations to an existing database.

    ALTER TABLE ... ADD COLUMN is used so pre-existing databases upgrade in
    place without losing data. Each guard makes this safe to run every startup.
    """
    if not _column_exists(conn, "cart_items", "attempts"):
        conn.execute("ALTER TABLE cart_items ADD COLUMN attempts INTEGER NOT NULL DEFAULT 0")
    if not _column_exists(conn, "cart_items", "error_message"):
        conn.execute("ALTER TABLE cart_items ADD COLUMN error_message TEXT")
    if not _column_exists(conn, "sarees", "image_type"):
        conn.execute("ALTER TABLE sarees ADD COLUMN image_type TEXT NOT NULL DEFAULT 'fabric'")
    if not _column_exists(conn, "cart_items", "pleat_style"):
        conn.execute("ALTER TABLE cart_items ADD COLUMN pleat_style TEXT NOT NULL DEFAULT 'single'")


def init_db():
    """Initialize database tables if they don't exist."""
    conn = get_db()
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS sarees (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            name        TEXT NOT NULL,
            code        TEXT NOT NULL,
            filename    TEXT NOT NULL,
            image_type  TEXT NOT NULL DEFAULT 'fabric',
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
            attempts    INTEGER NOT NULL DEFAULT 0,
            error_message TEXT,
            pleat_style TEXT NOT NULL DEFAULT 'single',
            FOREIGN KEY (session_id) REFERENCES sessions(id),
            FOREIGN KEY (saree_id)   REFERENCES sarees(id)
        );

        -- Shared, restart-safe key/value state (e.g. which saree the display shows).
        CREATE TABLE IF NOT EXISTS app_state (
            key   TEXT PRIMARY KEY,
            value TEXT
        );
    """)
    _migrate(conn)
    conn.commit()
    conn.close()


def create_session() -> int:
    """Create a new session. Ends any previously active sessions first.
    Returns the new session ID."""
    conn = get_db()
    # End all currently active sessions so only one is active at a time
    conn.execute("UPDATE sessions SET status = 'ended' WHERE status = 'active'")
    cursor = conn.execute("INSERT INTO sessions (status) VALUES ('active')")
    session_id = cursor.lastrowid
    conn.commit()
    conn.close()
    print(f"[INFO] Created new session: {session_id}")
    return session_id


def set_customer_photo(session_id: int, filename: str):
    """Set the customer photo filename for a session."""
    conn = get_db()
    conn.execute(
        "UPDATE sessions SET customer_photo = ? WHERE id = ?",
        (filename, session_id)
    )
    conn.commit()
    conn.close()
    print(f"[INFO] Set customer photo for session {session_id}: {filename}")


def get_active_session() -> dict | None:
    """Return the most recent active session, or None if no active session."""
    conn = get_db()
    row = conn.execute(
        "SELECT * FROM sessions WHERE status = 'active' ORDER BY id DESC LIMIT 1"
    ).fetchone()
    conn.close()
    if row:
        return dict(row)
    return None


def end_session(session_id: int):
    """End a session by setting its status to 'ended'."""
    conn = get_db()
    conn.execute(
        "UPDATE sessions SET status = 'ended' WHERE id = ?",
        (session_id,)
    )
    conn.commit()
    conn.close()
    print(f"[INFO] Ended session: {session_id}")


def clear_customer_photo(session_id: int):
    """Forget a session's customer photograph reference.

    Called when a session ends, alongside deleting the file itself: the
    customer's own photo is input data we have no reason to keep.
    """
    conn = get_db()
    conn.execute("UPDATE sessions SET customer_photo = NULL WHERE id = ?", (session_id,))
    conn.commit()
    conn.close()
    print(f"[INFO] Cleared customer photo reference for session {session_id}")


def get_all_sarees() -> list[dict]:
    """Return all sarees in the catalog."""
    conn = get_db()
    rows = conn.execute(
        "SELECT id, name, code, filename, image_type, created_at FROM sarees ORDER BY id DESC"
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def add_saree(name: str, code: str, filename: str, image_type: str = "fabric") -> int:
    """Add a new saree to the catalog. Returns the new saree ID.

    image_type is 'fabric' (flat-lay / garment-only photo) or 'model'
    (someone already wearing the saree) — used to route try-on processing.
    """
    if image_type not in ("fabric", "model"):
        image_type = "fabric"
    conn = get_db()
    cursor = conn.execute(
        "INSERT INTO sarees (name, code, filename, image_type) VALUES (?, ?, ?, ?)",
        (name, code, filename, image_type)
    )
    saree_id = cursor.lastrowid
    conn.commit()
    conn.close()
    print(f"[INFO] Added saree: {name} ({code}) [{image_type}] -> id={saree_id}")
    return saree_id


def add_to_cart(session_id: int, saree_id: int, pleat_style: str = "single") -> int:
    """Add a saree to the session cart. Enforces max 20 items.

    pleat_style ('single' | 'double') is the drape style staff chose for this
    try-on; it is persisted so a crash-recovered job keeps the same choice.
    Returns the new cart_item ID. Raises ValueError if limit exceeded."""
    if pleat_style not in ("single", "double"):
        pleat_style = "single"
    conn = get_db()

    # Check current cart size
    count = conn.execute(
        "SELECT COUNT(*) FROM cart_items WHERE session_id = ?",
        (session_id,)
    ).fetchone()[0]

    if count >= 20:
        conn.close()
        raise ValueError("Cart limit reached: maximum 20 sarees per session")

    # Check if saree is already in cart for this session
    existing = conn.execute(
        "SELECT id FROM cart_items WHERE session_id = ? AND saree_id = ?",
        (session_id, saree_id)
    ).fetchone()

    if existing:
        conn.close()
        raise ValueError("Saree is already in the cart")

    cursor = conn.execute(
        "INSERT INTO cart_items (session_id, saree_id, status, pleat_style) "
        "VALUES (?, ?, 'pending', ?)",
        (session_id, saree_id, pleat_style)
    )
    cart_item_id = cursor.lastrowid
    conn.commit()
    conn.close()
    print(f"[INFO] Added to cart: session={session_id}, saree={saree_id}, cart_item={cart_item_id}")
    return cart_item_id


def get_cart(session_id: int) -> list[dict]:
    """Return all cart items for a session, joined with saree data."""
    conn = get_db()
    rows = conn.execute("""
        SELECT
            c.id,
            c.session_id,
            c.saree_id,
            c.result_path,
            c.status,
            c.attempts,
            c.error_message,
            c.pleat_style,
            s.name AS saree_name,
            s.code AS saree_code,
            s.filename AS saree_filename,
            s.image_type AS saree_image_type
        FROM cart_items c
        JOIN sarees s ON c.saree_id = s.id
        WHERE c.session_id = ?
        ORDER BY c.id ASC
    """, (session_id,)).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def set_result(cart_item_id: int, result_path: str, status: str = "done",
               error_message: str | None = None):
    """Update the result path, status, and optional error message for a cart item."""
    conn = get_db()
    conn.execute(
        "UPDATE cart_items SET result_path = ?, status = ?, error_message = ? WHERE id = ?",
        (result_path, status, error_message, cart_item_id)
    )
    conn.commit()
    conn.close()
    print(f"[INFO] Set result for cart_item {cart_item_id}: {result_path} ({status})")


def mark_processing(cart_item_id: int) -> int:
    """Mark a cart item as actively processing and increment its attempt count.
    Returns the new attempt number."""
    conn = get_db()
    conn.execute(
        "UPDATE cart_items SET status = 'processing', attempts = attempts + 1 WHERE id = ?",
        (cart_item_id,)
    )
    row = conn.execute(
        "SELECT attempts FROM cart_items WHERE id = ?", (cart_item_id,)
    ).fetchone()
    conn.commit()
    conn.close()
    return row["attempts"] if row else 0


def get_incomplete_cart_items(max_attempts: int) -> list[dict]:
    """Return cart items that need (re)processing after a restart.

    Only items belonging to a still-active session, whose session already has a
    customer photo, and which have not yet exhausted their retry budget. Joined
    with the saree filename and the session's customer photo so the worker can
    run them directly.
    """
    conn = get_db()
    rows = conn.execute("""
        SELECT
            c.id,
            c.session_id,
            c.saree_id,
            c.status,
            c.attempts,
            c.pleat_style,
            s.name AS saree_name,
            s.filename AS saree_filename,
            s.image_type AS saree_image_type,
            sess.customer_photo AS customer_photo
        FROM cart_items c
        JOIN sarees s   ON c.saree_id = s.id
        JOIN sessions sess ON c.session_id = sess.id
        WHERE c.status IN ('pending', 'processing')
          AND sess.status = 'active'
          AND sess.customer_photo IS NOT NULL
          AND c.attempts < ?
        ORDER BY c.id ASC
    """, (max_attempts,)).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_cart_item(cart_item_id: int) -> dict | None:
    """Return a single cart item with joined saree data."""
    conn = get_db()
    row = conn.execute("""
        SELECT
            c.id,
            c.session_id,
            c.saree_id,
            c.result_path,
            c.status,
            c.attempts,
            c.error_message,
            c.pleat_style,
            s.name AS saree_name,
            s.code AS saree_code,
            s.filename AS saree_filename,
            s.image_type AS saree_image_type
        FROM cart_items c
        JOIN sarees s ON c.saree_id = s.id
        WHERE c.id = ?
    """, (cart_item_id,)).fetchone()
    conn.close()
    if row:
        return dict(row)
    return None


_DISPLAY_KEY = "display_cart_item_id"


def set_display_selection(cart_item_id: int | None):
    """Persist which cart item the display screen is currently showing.

    Stored in the DB (not an in-process global) so it is shared across workers
    and survives a restart. Passing None clears the selection.
    """
    conn = get_db()
    if cart_item_id is None:
        conn.execute("DELETE FROM app_state WHERE key = ?", (_DISPLAY_KEY,))
    else:
        conn.execute(
            "INSERT INTO app_state (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (_DISPLAY_KEY, str(cart_item_id))
        )
    conn.commit()
    conn.close()


def get_display_selection() -> int | None:
    """Return the cart item id the display is showing, or None if unset."""
    conn = get_db()
    row = conn.execute(
        "SELECT value FROM app_state WHERE key = ?", (_DISPLAY_KEY,)
    ).fetchone()
    conn.close()
    if row and row["value"] is not None:
        try:
            return int(row["value"])
        except (TypeError, ValueError):
            return None
    return None


def delete_saree(saree_id: int) -> str | None:
    """Delete a saree from the database. Returns the filename of the saree if found."""
    conn = get_db()
    row = conn.execute("SELECT filename FROM sarees WHERE id = ?", (saree_id,)).fetchone()
    if row:
        filename = row["filename"]
        # Delete associated cart items first
        conn.execute("DELETE FROM cart_items WHERE saree_id = ?", (saree_id,))
        conn.execute("DELETE FROM sarees WHERE id = ?", (saree_id,))
        conn.commit()
        conn.close()
        print(f"[INFO] Deleted saree id {saree_id} ({filename}) from database")
        return filename
    conn.close()
    return None

