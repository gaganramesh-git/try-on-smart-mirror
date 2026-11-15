#!/usr/bin/env bash
# First-run setup for GitHub Codespaces.
set -euo pipefail

pip install --quiet -r requirements.txt

if [ ! -f .env ]; then
  # Never run a forwarded port on the example's guessable defaults:
  # generate a random secret key and a random 4-digit staff PIN.
  python3 - <<'PY'
import re, secrets

pin = f"{secrets.randbelow(10000):04d}"
text = open(".env.example").read()
text = re.sub(r"(?m)^FLASK_SECRET_KEY=.*$", f"FLASK_SECRET_KEY={secrets.token_hex(32)}", text)
text = re.sub(r"(?m)^STAFF_PIN=.*$", f"STAFF_PIN={pin}", text)
open(".env", "w").write(text)
print(f"\n  Staff PIN for this Codespace: {pin}   (stored in .env)\n")
PY
fi

mkdir -p static/uploads static/results
