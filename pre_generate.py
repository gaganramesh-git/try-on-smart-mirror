# pre_generate.py — Run this before a demo to pre-generate all try-on results
# Usage: python pre_generate.py <customer_photo_path>
#
# This script processes every saree in the catalog against the given customer
# photo, so results are cached and load instantly during the demo.

import sys
import db
import tryon


def main():
    if len(sys.argv) < 2:
        print("Usage: python pre_generate.py <customer_photo_path>")
        print("Example: python pre_generate.py static/uploads/my_photo.jpg")
        sys.exit(1)

    customer_photo = sys.argv[1]

    # Initialize the database
    db.init_db()

    sarees = db.get_all_sarees()
    if not sarees:
        print("[ERROR] No sarees found in catalog. Upload sarees via the admin panel first.")
        sys.exit(1)

    print(f"Generating try-ons for {len(sarees)} sarees...")
    print(f"Customer photo: {customer_photo}")
    print(f"Backend: {tryon.BACKEND}")
    print("-" * 40)

    success_count = 0
    fail_count = 0

    for i, s in enumerate(sarees, 1):
        saree_path = f"static/sarees/{s['filename']}"
        print(f"\n[{i}/{len(sarees)}] Processing: {s['name']} ({s['code']})")
        try:
            result = tryon.run_tryon(customer_photo, saree_path)
            print(f"  ✓ Done: {result}")
            success_count += 1
        except Exception as e:
            print(f"  ✗ Failed: {e}")
            fail_count += 1

    print("\n" + "=" * 40)
    print(f"Results: {success_count} succeeded, {fail_count} failed")
    print("=" * 40)


if __name__ == "__main__":
    main()
