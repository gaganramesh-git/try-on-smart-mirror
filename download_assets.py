import requests
import sqlite3
import os
import uuid
from pathlib import Path

# Paths
DB_PATH = "saree_tryon.db"
SAREE_DIR = Path("static/sarees")
UPLOAD_DIR = Path("static/uploads")
SAREE_DIR.mkdir(parents=True, exist_ok=True)
UPLOAD_DIR.mkdir(parents=True, exist_ok=True)

# Saree assets to download (Unsplash high quality, free-to-use photos of sarees and fabrics)
SAREES = [
    {
        "name": "Royal Crimson Banarasi",
        "code": "SLK-BAN-CRM01",
        "url": "https://images.unsplash.com/photo-1610030469983-98e550d6193c?q=80&w=800&auto=format&fit=crop"
    },
    {
        "name": "Emerald Gold Kanjeevaram",
        "code": "SLK-KAN-EMD02",
        "url": "https://images.unsplash.com/photo-1617627143750-d86bc21e42bb?q=80&w=800&auto=format&fit=crop"
    },
    {
        "name": "Midnight Indigo Georgette",
        "code": "GEO-IND-MID03",
        "url": "https://images.unsplash.com/photo-1583391733956-3750e0ff4e8b?q=80&w=800&auto=format&fit=crop"
    }
]

# High quality Wikipedia portrait of Aishwarya Rai Bachchan (Cannes 2017)
AISHWARYA_URL = "https://upload.wikimedia.org/wikipedia/commons/3/3a/Aishwarya_Rai_Cannes_2017.jpg"

def download_file(url, dest_path):
    print(f"Downloading: {url} -> {dest_path}")
    response = requests.get(url, headers={'User-Agent': 'Mozilla/5.0'}, verify=False, timeout=30)
    response.raise_for_status()
    with open(dest_path, 'wb') as out_file:
        out_file.write(response.content)
    print("Download complete.")

def populate_demo():
    print("=" * 60)
    print("  Populating Saree Try-On Demo Assets & Session")
    print("=" * 60)

    import urllib3
    urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()

    # 1. Clear any existing records to start fresh
    cursor.execute("DELETE FROM cart_items")
    cursor.execute("DELETE FROM sarees")
    cursor.execute("DELETE FROM sessions")
    conn.commit()

    # 2. Download and insert Sarees
    for index, saree_info in enumerate(SAREES):
        filename = f"{uuid.uuid4().hex}_saree.jpg"
        dest = SAREE_DIR / filename
        try:
            download_file(saree_info["url"], dest)
            cursor.execute(
                "INSERT INTO sarees (name, code, filename) VALUES (?, ?, ?)",
                (saree_info["name"], saree_info["code"], filename)
            )
            print(f"[OK] Added Saree: {saree_info['name']}")
        except Exception as e:
            print(f"[ERROR] Failed to download {saree_info['name']}: {e}")

    # 3. Download Aishwarya Rai's picture
    aishwarya_filename = f"aishwarya_rai_{uuid.uuid4().hex}.jpg"
    aishwarya_dest = UPLOAD_DIR / aishwarya_filename
    try:
        download_file(AISHWARYA_URL, aishwarya_dest)
        print("[OK] Downloaded Aishwarya Rai portrait image.")
    except Exception as e:
        print(f"[ERROR] Failed to download Aishwarya portrait: {e}")
        aishwarya_filename = None

    # 4. Create an active session and pre-link Aishwarya Rai's photo
    cursor.execute("INSERT INTO sessions (customer_photo, status) VALUES (?, 'active')", (aishwarya_filename,))
    session_id = cursor.lastrowid
    conn.commit()
    conn.close()

    print("\n" + "=" * 60)
    print("  DEMO PRE-POPULATION COMPLETED SUCCESSFULLY!")
    print(f"  Active Session ID: {session_id}")
    if aishwarya_filename:
        print(f"  Customer Photo: static/uploads/{aishwarya_filename}")
    print("=" * 60)

if __name__ == "__main__":
    populate_demo()
