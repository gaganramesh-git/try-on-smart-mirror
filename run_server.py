import os

import app


if __name__ == "__main__":
    # Honour a PORT from the environment (used by the preview harness / autoPort),
    # falling back to 5001 for a plain local run.
    port = int(os.environ.get("PORT", "5001"))
    print("=" * 60)
    print("  Saree Virtual Try-On Server")
    print("  ----------------------------")
    print(f"  Backend: {app.tryon.BACKEND}")
    print(f"  Admin:   http://localhost:{port}/admin")
    print(f"  Kiosk:   http://localhost:{port}/kiosk")
    print(f"  Display: http://localhost:{port}/display")
    print("=" * 60)
    app.socketio.run(
        app.app,
        host="0.0.0.0",
        port=port,
        debug=False,
        allow_unsafe_werkzeug=True,
    )
