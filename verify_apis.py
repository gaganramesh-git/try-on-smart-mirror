import requests
import io
import time
from PIL import Image

BASE_URL = "http://127.0.0.1:5001"

def create_dummy_image(color=(128, 0, 128), size=(100, 100)):
    """Create an in-memory dummy image for testing uploads."""
    file = io.BytesIO()
    image = Image.new("RGB", size, color)
    image.save(file, "JPEG")
    file.seek(0)
    return file

def run_verification():
    print("=" * 60)
    print("  Saree Virtual Try-On API Verification Script")
    print("=" * 60)

    # 1. Check initial active session status
    print("\n[STEP 1] Checking active session...")
    res = requests.get(f"{BASE_URL}/kiosk/active-session")
    assert res.status_code == 200, f"Failed: {res.text}"
    session_data = res.json()
    print(f"  [OK] Session status checked successfully. Active: {session_data.get('active')}")

    # 2. Start a new session
    print("\n[STEP 2] Starting a new store session...")
    res = requests.post(f"{BASE_URL}/admin/new-session")
    assert res.status_code == 200, f"Failed: {res.text}"
    session_id = res.json()["session_id"]
    print(f"  [OK] Store session started successfully. Session ID: {session_id}")

    # 3. Verify active session is active and photo is null
    print("\n[STEP 3] Verifying active session state...")
    res = requests.get(f"{BASE_URL}/kiosk/active-session")
    assert res.status_code == 200, f"Failed: {res.text}"
    active_data = res.json()
    assert active_data["active"] is True
    assert active_data["customer_photo"] is None
    print("  [OK] Active session state matches expectations.")

    # 4. Upload a dummy customer photo
    print("\n[STEP 4] Uploading customer photo...")
    customer_photo_file = create_dummy_image(color=(200, 200, 200), size=(600, 800))
    res = requests.post(
        f"{BASE_URL}/kiosk/photo",
        files={"photo": ("customer.jpg", customer_photo_file, "image/jpeg")}
    )
    assert res.status_code == 200, f"Failed: {res.text}"
    upload_data = res.json()
    print(f"  [OK] Customer photo uploaded. Filename: {upload_data['filename']}")

    # 5. Verify customer photo is linked to active session
    print("\n[STEP 5] Verifying photo linkage...")
    res = requests.get(f"{BASE_URL}/kiosk/active-session")
    assert res.status_code == 200, f"Failed: {res.text}"
    active_data = res.json()
    assert active_data["customer_photo"] == upload_data["filename"]
    print(f"  [OK] Customer photo successfully linked. Photo URL: {active_data['photo_url']}")

    # 6. Upload a dummy saree to the catalog
    print("\n[STEP 6] Uploading dummy saree to catalog...")
    saree_image_file = create_dummy_image(color=(180, 20, 50), size=(400, 600))
    res = requests.post(
        f"{BASE_URL}/admin/upload-saree",
        data={"name": "Crimson Silk Saree", "code": "SLK-CRM-01"},
        files={"image": ("saree.jpg", saree_image_file, "image/jpeg")}
    )
    assert res.status_code == 200, f"Failed: {res.text}"
    saree_data = res.json()
    saree_id = saree_data["id"]
    print(f"  [OK] Saree uploaded. ID: {saree_id}, Name: {saree_data['name']}")

    # 7. Get catalog and verify our new saree is present with image_url
    print("\n[STEP 7] Fetching saree catalog...")
    res = requests.get(f"{BASE_URL}/kiosk/sarees")
    assert res.status_code == 200, f"Failed: {res.text}"
    catalog = res.json()
    found_saree = next((s for s in catalog if s["id"] == saree_id), None)
    assert found_saree is not None, "Uploaded saree not found in catalog listing!"
    assert "image_url" in found_saree, "image_url is missing from saree object!"
    print(f"  [OK] Catalog loaded. Found our saree with image_url: {found_saree['image_url']}")

    # 8. Add saree to try-on cart (starts background task)
    print("\n[STEP 8] Adding saree to try-on cart...")
    res = requests.post(
        f"{BASE_URL}/kiosk/cart/add",
        json={"saree_id": saree_id}
    )
    assert res.status_code == 200, f"Failed: {res.text}"
    cart_data = res.json()
    cart_item_id = cart_data["cart_item_id"]
    print(f"  [OK] Added to cart. Cart Item ID: {cart_item_id}, Status: {cart_data['status']}")

    # 9. Select the cart item (triggering display screen)
    print("\n[STEP 9] Simulating display selection...")
    res = requests.post(
        f"{BASE_URL}/kiosk/cart/select",
        json={"cart_item_id": cart_item_id}
    )
    assert res.status_code == 200, f"Failed: {res.text}"
    select_data = res.json()
    print(f"  [OK] Cart item selected. Status: {select_data['status']}, Current Result: {select_data['result_url']}")

    # 10. Wait for background try-on process to complete and check status
    print("\n[STEP 10] Waiting for background try-on process to complete...")
    for attempt in range(120):
        time.sleep(1)
        res = requests.get(f"{BASE_URL}/result-status/{cart_item_id}")
        assert res.status_code == 200, f"Failed: {res.text}"
        status_data = res.json()
        print(f"  Attempt {attempt + 1}: Status = {status_data['status']}")
        if status_data["status"] == "done":
            print(f"  [OK] Virtual try-on finished! Result URL: {status_data['result_url']}")
            assert status_data["result_url"] is not None
            break
    else:
        raise RuntimeError("Try-on did not finish in time!")

    # 11. End the active session
    print("\n[STEP 11] Ending active session...")
    res = requests.post(f"{BASE_URL}/admin/end-session")
    assert res.status_code == 200, f"Failed: {res.text}"
    print("  [OK] Active session ended.")

    # 12. Delete the saree from catalog and verify clean up
    print("\n[STEP 12] Deleting saree from catalog...")
    res = requests.delete(f"{BASE_URL}/admin/delete-saree/{saree_id}")
    assert res.status_code == 200, f"Failed: {res.text}"
    print("  [OK] Saree deleted from catalog.")

    # 13. Verify saree is no longer in catalog
    res = requests.get(f"{BASE_URL}/kiosk/sarees")
    catalog_after = res.json()
    saree_exists = any(s["id"] == saree_id for s in catalog_after)
    assert not saree_exists, "Saree still exists in catalog after deletion!"
    print("  [OK] Verified saree is gone from catalog.")

    print("\n" + "=" * 60)
    print("  * ALL API VERIFICATIONS PASSED SUCCESSFULLY! *")
    print("=" * 60)

if __name__ == "__main__":
    run_verification()
