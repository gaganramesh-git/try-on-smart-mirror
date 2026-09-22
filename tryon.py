import os
import uuid
import time
import json
from pathlib import Path
from dotenv import load_dotenv
from PIL import Image

load_dotenv()

RESULTS_DIR = Path("static/results")
RESULTS_DIR.mkdir(parents=True, exist_ok=True)

BACKEND = os.getenv("TRYON_BACKEND", "kolors")
KOLORS_SPACE_URL = os.getenv(
    "KOLORS_SPACE_URL",
    "https://kwai-kolors-kolors-virtual-try-on.hf.space",
).rstrip("/")
KOLORS_SEED = int(os.getenv("KOLORS_SEED", "42"))
KOLORS_RANDOMIZE_SEED = os.getenv("KOLORS_RANDOMIZE_SEED", "false").lower() == "true"
KOLORS_MAX_RETRIES = int(os.getenv("KOLORS_MAX_RETRIES", "3"))
KOLORS_RETRY_DELAY_SECONDS = int(os.getenv("KOLORS_RETRY_DELAY_SECONDS", "20"))

# Generative backend (Google Gemini image model). Handles fabric-only sarees by
# synthesizing the drape from the prompt rather than needing a model-worn photo.
GEMINI_IMAGE_MODEL = os.getenv("GEMINI_IMAGE_MODEL", "gemini-2.5-flash-image")

# fal.ai backend running Qwen-Image-Edit-Plus: a multi-image instruction-edit
# model (customer photo + saree fabric + drape prompt). Like the Gemini path it
# synthesizes the drape, so fabric-only saree photos work without a model shot.
FAL_KEY = os.getenv("FAL_KEY", "").strip()
FAL_MODEL = os.getenv("FAL_MODEL", "fal-ai/qwen-image-edit-plus")


class PermanentTryonError(RuntimeError):
    """A try-on failure that will not succeed on retry (quota, billing, or missing
    configuration). The worker surfaces it immediately instead of burning retries."""


def _prepare_customer_image(photo_path: str) -> str:
    """Resize and pad customer photo to 768x1024 (3:4 aspect ratio).
    Returns the path to the prepared image."""
    img = Image.open(photo_path)
    img = img.convert("RGB")

    target_w, target_h = 768, 1024
    # Calculate scaling to fit within target while maintaining aspect ratio
    ratio = min(target_w / img.width, target_h / img.height)
    new_w = int(img.width * ratio)
    new_h = int(img.height * ratio)
    img = img.resize((new_w, new_h), Image.LANCZOS)

    # Pad to exact target size with white background
    padded = Image.new("RGB", (target_w, target_h), (255, 255, 255))
    paste_x = (target_w - new_w) // 2
    paste_y = (target_h - new_h) // 2
    padded.paste(img, (paste_x, paste_y))

    prepared_path = photo_path.rsplit(".", 1)[0] + "_prepared.jpg"
    padded.save(prepared_path, "JPEG", quality=95)
    return prepared_path


def _prepare_saree_image(saree_path: str) -> str:
    """Resize saree image to 768x1024 for API input.
    Returns path to the prepared image."""
    img = Image.open(saree_path)
    img = img.convert("RGB")

    target_w, target_h = 768, 1024
    ratio = min(target_w / img.width, target_h / img.height)
    new_w = int(img.width * ratio)
    new_h = int(img.height * ratio)
    img = img.resize((new_w, new_h), Image.LANCZOS)

    padded = Image.new("RGB", (target_w, target_h), (255, 255, 255))
    paste_x = (target_w - new_w) // 2
    paste_y = (target_h - new_h) // 2
    padded.paste(img, (paste_x, paste_y))

    prepared_path = saree_path.rsplit(".", 1)[0] + "_prepared.jpg"
    padded.save(prepared_path, "JPEG", quality=95)
    return prepared_path


def run_tryon(customer_photo_path: str, saree_image_path: str,
              image_type: str | None = None, saree_name: str | None = None,
              pleat_style: str | None = None) -> str:
    """
    Main entry point. Returns the local file path of the result image.
    Raises RuntimeError on failure.

    image_type ('fabric' | 'model') lets 'auto' mode route each saree to the
    backend that handles it best. saree_name is passed to the generative prompt.
    pleat_style ('single' | 'double') selects the pallu drape the generative
    backends synthesize; ignored by the warp-based Kolors backend, which can only
    reproduce the drape present in a model-worn photo.
    """
    backend = BACKEND
    # 'auto' routes each saree to the backend that actually handles it well:
    #
    #   model-worn photo -> Kolors. It warps a garment already being worn onto the
    #       customer, which is exactly this case, and it is free.
    #   fabric/flat-lay  -> fal (Qwen). Kolors cannot INVENT a drape from a flat
    #       piece of cloth — measured behaviour is a gown or a top+trouser set, not
    #       pleats and a pallu. Only a generative model constructs the drape.
    #
    # Without a FAL_KEY there is no generative option, so fabric falls back to
    # Kolors as best effort — degraded, and logged as such rather than failing.
    # (Gemini is deliberately excluded: its image model has zero free-tier quota.)
    if backend == "auto":
        if image_type == "fabric" and FAL_KEY:
            backend = "fal"
        elif image_type == "fabric":
            print("[WARNING] Fabric-only saree with no FAL_KEY — falling back to "
                  "Kolors. Expect a poor drape; set FAL_KEY for a real one.")
            backend = "kolors"
        else:
            backend = "kolors"

    print(f"[INFO] Running try-on: backend={backend} image_type={image_type}")
    print(f"[INFO]   Customer photo: {customer_photo_path}")
    print(f"[INFO]   Saree image: {saree_image_path}")

    if backend == "kolors":
        return _tryon_kolors(customer_photo_path, saree_image_path)
    elif backend in ("generative", "gemini"):
        return _tryon_generative(customer_photo_path, saree_image_path, saree_name, pleat_style)
    elif backend in ("fal", "qwen"):
        return _tryon_fal(customer_photo_path, saree_image_path, saree_name, pleat_style)
    elif backend == "mock":
        return _tryon_mock(customer_photo_path, saree_image_path)
    else:
        raise ValueError(f"Unknown TRYON_BACKEND: {backend}")


def _tryon_kolors(customer_photo_path: str, saree_image_path: str) -> str:
    """
    Calls Kwai-Kolors/Kolors-Virtual-Try-On via its Gradio queue.
    The Space does not expose a normal named API, so this uses the public queue
    function behind the Run button.
    """
    import requests

    prepared_customer = _prepare_customer_image(customer_photo_path)
    prepared_saree = _prepare_saree_image(saree_image_path)

    try:
        session = requests.Session()
        session.headers.update({"User-Agent": "saree-tryon-local/1.0"})

        def upload_file(path: str) -> dict:
            with open(path, "rb") as f:
                files = {"files": (Path(path).name, f, "image/jpeg")}
                response = session.post(f"{KOLORS_SPACE_URL}/upload", files=files, timeout=90)
            response.raise_for_status()
            uploaded = response.json()
            uploaded_path = uploaded[0] if isinstance(uploaded, list) else uploaded
            return {
                "path": uploaded_path,
                "url": f"{KOLORS_SPACE_URL}/file={uploaded_path}",
                "orig_name": Path(path).name,
                "mime_type": "image/jpeg",
                "meta": {"_type": "gradio.FileData"},
            }

        person_file = upload_file(prepared_customer)
        garment_file = upload_file(prepared_saree)
        output_file = None
        last_error = None

        for attempt in range(1, KOLORS_MAX_RETRIES + 1):
            session_hash = uuid.uuid4().hex
            payload = {
                "data": [person_file, garment_file, KOLORS_SEED, KOLORS_RANDOMIZE_SEED],
                "event_data": None,
                "fn_index": 2,
                "trigger_id": 26,
                "session_hash": session_hash,
            }

            print(f"[INFO] Sending request to Kolors Space: {KOLORS_SPACE_URL} (attempt {attempt}/{KOLORS_MAX_RETRIES}) ...")
            join_response = session.post(f"{KOLORS_SPACE_URL}/queue/join", json=payload, timeout=90)
            join_response.raise_for_status()

            started = time.time()
            with session.get(
                f"{KOLORS_SPACE_URL}/queue/data",
                params={"session_hash": session_hash},
                stream=True,
                timeout=260,
            ) as response:
                response.raise_for_status()
                for raw_line in response.iter_lines(decode_unicode=True):
                    if not raw_line:
                        if time.time() - started > 240:
                            last_error = "Kolors request timed out"
                            break
                        continue
                    if not raw_line.startswith("data: "):
                        continue
                    message = json.loads(raw_line[6:])
                    msg = message.get("msg")
                    if msg == "process_completed":
                        if not message.get("success"):
                            last_error = message.get("output", {}).get("error") or str(message)
                            break
                        data = message.get("output", {}).get("data", [])
                        if data and isinstance(data[0], dict) and data[0].get("path"):
                            output_file = data[0]
                        break
                    if msg in {"queue_full", "unexpected_error"}:
                        last_error = str(message)
                        break

            if output_file:
                break

            if attempt < KOLORS_MAX_RETRIES:
                print(f"[WARNING] Kolors attempt failed: {last_error}. Retrying in {KOLORS_RETRY_DELAY_SECONDS}s...")
                time.sleep(KOLORS_RETRY_DELAY_SECONDS)

        if not output_file:
            raise RuntimeError(f"Kolors did not return an output image: {last_error}")

        output_filename = f"{uuid.uuid4().hex}.png"
        output_path = RESULTS_DIR / output_filename
        output_url = output_file.get("url") or f"{KOLORS_SPACE_URL}/file={output_file['path']}"
        image_response = session.get(output_url, timeout=90)
        image_response.raise_for_status()
        output_path.write_bytes(image_response.content)

        print(f"[INFO] Kolors try-on complete: {output_path}")
        return str(output_path)
    finally:
        for p in [prepared_customer, prepared_saree]:
            try:
                os.remove(p)
            except OSError:
                pass


def _saree_drape_prompt(saree_name: str | None,
                        pleat_style: str | None = None) -> str:
    """Build a saree-specific draping instruction for the generative model.

    Written so a flat-lay / fabric-only saree image works: the model is told to
    treat image 2 as the cloth and synthesize the traditional drape itself.

    pleat_style ('single' | 'double') controls how the pallu is pleated over the
    shoulder; anything else falls back to the single-pleat description.
    """
    named = f' The saree is called "{saree_name}".' if saree_name else ""
    if pleat_style == "double":
        pallu = (
            "Drape the pallu in the DOUBLE-PLEAT style: gather the pallu into two "
            "layered, neatly folded sets of pleats and pin them over the LEFT "
            "shoulder for a fuller, more voluminous pallu that falls to the back."
        )
    else:
        pallu = (
            "Drape the pallu in the SINGLE-PLEAT style: gather the pallu into one "
            "set of neat, evenly folded pleats pinned flat over the LEFT shoulder, "
            "falling straight down the back."
        )
    return (
        "You are a virtual try-on system for Indian sarees. "
        "IMAGE 1 is a full-body photo of a person. "
        "IMAGE 2 is a saree — it may be a flat-lay or fabric-only photo showing just "
        "the cloth: its colour, border and pattern." + named + " "
        "Generate ONE photorealistic image of the SAME person from IMAGE 1 now wearing "
        "this saree, draped in the traditional Nivi style: neat pleats tucked at the "
        "waist and falling to the ankles, plus a matching fitted blouse. " + pallu + " "
        "Use the exact colour, border, texture and motifs from IMAGE 2 for the fabric. "
        "STRICTLY preserve the person's face, hairstyle, skin tone, body proportions, "
        "height, pose and the background from IMAGE 1 — change ONLY the clothing. "
        "Full-body framing, natural lighting, realistic fabric folds. Return only the image."
    )


def _tryon_fal(customer_photo_path: str, saree_image_path: str,
               saree_name: str | None = None,
               pleat_style: str | None = None) -> str:
    """
    Generative saree try-on via fal.ai running Qwen-Image-Edit-Plus.

    Sends BOTH the customer photo and the saree fabric as inputs (the model
    accepts multiple images) plus the draping instruction, so a fabric-only
    saree photo works without any model-worn reference.
    Returns the local path to the saved result image.
    """
    import fal_client
    import requests

    if not FAL_KEY:
        raise PermanentTryonError("FAL_KEY is not configured — set it to use the fal backend")
    # fal_client reads credentials from the environment.
    os.environ["FAL_KEY"] = FAL_KEY

    prepared_customer = _prepare_customer_image(customer_photo_path)
    prepared_saree = _prepare_saree_image(saree_image_path)

    try:
        # Upload local files to fal storage — the API takes image URLs.
        customer_url = fal_client.upload_file(prepared_customer)
        saree_url = fal_client.upload_file(prepared_saree)

        print(f"[INFO] Sending request to fal model: {FAL_MODEL}")
        result = fal_client.subscribe(
            FAL_MODEL,
            arguments={
                "prompt": _saree_drape_prompt(saree_name, pleat_style),
                # Order matters: image 1 = person, image 2 = saree fabric.
                "image_urls": [customer_url, saree_url],
                "num_images": 1,
                "output_format": "png",
                "image_size": {"width": 768, "height": 1024},
            },
        )

        images = (result or {}).get("images") or []
        if not images or not images[0].get("url"):
            raise RuntimeError(f"fal returned no image (response: {str(result)[:200]})")

        output_filename = f"{uuid.uuid4().hex}.png"
        output_path = RESULTS_DIR / output_filename
        image_response = requests.get(images[0]["url"], timeout=120)
        image_response.raise_for_status()
        output_path.write_bytes(image_response.content)

        print(f"[INFO] fal try-on complete: {output_path}")
        return str(output_path)
    finally:
        for p in (prepared_customer, prepared_saree):
            try:
                os.remove(p)
            except OSError:
                pass


def _tryon_generative(customer_photo_path: str, saree_image_path: str,
                      saree_name: str | None = None,
                      pleat_style: str | None = None) -> str:
    """
    Generative saree try-on via a Google Gemini image model.

    Unlike garment-to-person VTON, this synthesizes the drape from the prompt, so
    it works from a fabric-only saree photo (no model-worn reference required).
    Returns the local path to the saved result image.
    """
    from google import genai
    from google.genai import types

    api_key = os.getenv("GEMINI_API_KEY", "").strip()
    if not api_key or api_key.lower().startswith("your_"):
        raise PermanentTryonError(
            "GEMINI_API_KEY is not configured — set a real key to use the generative backend"
        )

    # Standardise inputs (full-frame person, cleanly presented fabric).
    prepared_customer = _prepare_customer_image(customer_photo_path)
    prepared_saree = _prepare_saree_image(saree_image_path)

    try:
        client = genai.Client(api_key=api_key)

        def _image_part(path: str):
            with open(path, "rb") as f:
                data = f.read()
            mime = "image/png" if path.lower().endswith(".png") else "image/jpeg"
            return types.Part.from_bytes(data=data, mime_type=mime)

        prompt = _saree_drape_prompt(saree_name, pleat_style)
        print(f"[INFO] Sending request to generative model: {GEMINI_IMAGE_MODEL}")
        try:
            response = client.models.generate_content(
                model=GEMINI_IMAGE_MODEL,
                contents=[prompt, _image_part(prepared_customer), _image_part(prepared_saree)],
            )
        except Exception as e:
            # A quota/billing 429 is permanent for this project (free tier gives the
            # image model a limit of 0), so translate the giant JSON into a short,
            # actionable message and flag it as non-retryable for the worker.
            msg = str(e)
            if "RESOURCE_EXHAUSTED" in msg or "429" in msg or "quota" in msg.lower():
                raise PermanentTryonError(
                    "Gemini image generation is unavailable on this API key "
                    "(free-tier quota is 0 — it needs paid billing). Configure a "
                    "FAL_KEY to use the fal/Qwen backend instead."
                ) from e
            raise

        for cand in getattr(response, "candidates", None) or []:
            content = getattr(cand, "content", None)
            for part in getattr(content, "parts", None) or []:
                inline = getattr(part, "inline_data", None)
                if inline and getattr(inline, "data", None):
                    output_filename = f"{uuid.uuid4().hex}.png"
                    output_path = RESULTS_DIR / output_filename
                    output_path.write_bytes(inline.data)
                    print(f"[INFO] Generative try-on complete: {output_path}")
                    return str(output_path)

        # No image returned — surface any text (often a safety refusal) for debugging.
        text = getattr(response, "text", None)
        raise RuntimeError(f"Generative model returned no image (response text: {text!r})")
    finally:
        for p in (prepared_customer, prepared_saree):
            try:
                os.remove(p)
            except OSError:
                pass


def _tryon_mock(customer_photo_path: str, saree_image_path: str) -> str:
    """
    Advanced shading-aware simulation engine.
    1. Resizes customer and saree images to 768x1024.
    2. Isolates the saree fabric by removing plain white/light borders.
    3. Builds a realistic soft-feathered human body drape silhouette mask.
    4. Extracts grayscale clothing crease and lighting maps from the customer's photo.
    5. Displaces and multiplies folds back onto the saree fabric to preserve contours.
    6. Overlays a gorgeous golden Banarasi border along the diagonal pallu and lower pleats.
    """
    import time
    from collections import deque
    import colorsys
    from PIL import Image, ImageDraw, ImageChops, ImageEnhance, ImageFilter

    # Simulate processing time (2 seconds)
    time.sleep(2)

    # 1. Load and standardise images
    customer_img = Image.open(customer_photo_path).convert("RGBA")
    saree_img = Image.open(saree_image_path).convert("RGBA")

    output_w, output_h = 768, 1024

    # Fit customer photo into canvas
    customer_ratio = min(output_w / customer_img.width, output_h / customer_img.height)
    cust_new_w = int(customer_img.width * customer_ratio)
    cust_new_h = int(customer_img.height * customer_ratio)
    customer_resized = customer_img.resize((cust_new_w, cust_new_h), Image.LANCZOS)

    canvas_customer = Image.new("RGBA", (output_w, output_h), (20, 20, 20, 255))
    paste_x = (output_w - cust_new_w) // 2
    paste_y = (output_h - cust_new_h) // 2
    canvas_customer.paste(customer_resized, (paste_x, paste_y))

    # 2. Extract saree fabric pattern by stripping white backgrounds.
    # Catalog images can be flat-lays or model photos; the lower-middle crop is
    # usually the most reliable fabric-only region for model photos.
    crop_box = (
        int(saree_img.width * 0.16),
        int(saree_img.height * 0.48),
        int(saree_img.width * 0.84),
        int(saree_img.height * 0.96),
    )
    saree_img = saree_img.crop(crop_box)

    # Create mask of non-white pixels
    saree_data = saree_img.load()
    saree_mask = Image.new("L", saree_img.size, 255)
    mask_data = saree_mask.load()
    
    # Strip any border/background close to white
    for y in range(saree_img.height):
        for x in range(saree_img.width):
            r, g, b, a = saree_data[x, y]
            if r > 230 and g > 230 and b > 230:
                mask_data[x, y] = 0
                
    saree_fabric = Image.new("RGBA", saree_img.size)
    saree_fabric.paste(saree_img, (0, 0), mask=saree_mask)
    
    fabric_bbox = saree_mask.getbbox()
    if fabric_bbox:
        saree_fabric = saree_fabric.crop(fabric_bbox)

    def extract_saree_palette(fabric: Image.Image) -> tuple[tuple[int, int, int], tuple[int, int, int]]:
        """Pick clean base/accent colors without copying a catalog model into output."""
        sample = fabric.convert("RGB").resize((96, 96), Image.BILINEAR)
        color_counts = sample.quantize(colors=12, method=Image.Quantize.MEDIANCUT).convert("RGB").getcolors(96 * 96)
        candidates = []
        for count, rgb in color_counts or []:
            r, g, b = rgb
            h, s, v = colorsys.rgb_to_hsv(r / 255, g / 255, b / 255)
            if v < 0.12 or s < 0.16:
                continue
            # Skip common skin-like beige/orange clusters from model catalog photos.
            if 0.03 <= h <= 0.13 and 0.18 <= s <= 0.65 and v >= 0.35:
                continue
            candidates.append((count * (0.65 + s), rgb, h, s, v))

        if not candidates:
            return (31, 64, 170), (42, 188, 202)

        candidates.sort(reverse=True)
        base = candidates[0][1]
        base_h = candidates[0][2]
        accent = None
        for _, rgb, h, s, v in candidates[1:]:
            hue_gap = abs(h - base_h)
            hue_gap = min(hue_gap, 1 - hue_gap)
            if hue_gap > 0.08 or v > candidates[0][4] + 0.12:
                accent = rgb
                break

        if accent is None:
            r, g, b = base
            h, s, v = colorsys.rgb_to_hsv(r / 255, g / 255, b / 255)
            accent_rgb = colorsys.hsv_to_rgb((h + 0.08) % 1, min(0.75, s * 0.7), min(1.0, v * 1.45))
            accent = tuple(int(c * 255) for c in accent_rgb)

        return base, accent

    base_color, accent_color = extract_saree_palette(saree_fabric)

    def make_fabric_sheet(fabric: Image.Image, size: tuple[int, int], base: tuple[int, int, int]) -> Image.Image:
        """Create clean fabric sheet using the ACTUAL saree catalog fabric texture/pattern.
        Preserves details and applies custom 3D fold shadings for natural draping."""
        target_w, target_h = size
        if fabric.width <= 0 or fabric.height <= 0:
            return Image.new("RGBA", size, (*base, 255))

        # Scale the fabric to fill the target canvas
        scale_w = target_w
        scale_h = int(fabric.height * (target_w / fabric.width))
        if scale_h < target_h:
            scale_h = target_h
            scale_w = int(fabric.width * (target_h / fabric.height))

        resized_fabric = fabric.resize((scale_w, scale_h), Image.LANCZOS)
        
        # Crop from center
        sheet = Image.new("RGBA", size)
        crop_x = (scale_w - target_w) // 2
        crop_y = (scale_h - target_h) // 2
        sheet.paste(resized_fabric, (-crop_x, -crop_y))

        # Apply soft shading folds on top of the fabric texture
        draw_sheet = ImageDraw.Draw(sheet, "RGBA")
        
        # Soft dark and light folds
        for x in range(-target_w, target_w * 2, 48):
            shade_val = 15 if (x // 48) % 2 == 0 else -15
            fold_color = (0, 0, 0, 35) if shade_val < 0 else (255, 255, 255, 15)
            draw_sheet.line([(x, 0), (x + target_w // 3, target_h)], fill=fold_color, width=16)

        # Soft curved folds
        for y in range(90, target_h, 150):
            draw_sheet.arc(
                [target_w // 5, y - 80, target_w + 80, y + 150],
                start=180,
                end=250,
                fill=(255, 255, 255, 12),
                width=3,
            )

        return sheet.convert("RGBA")

    saree_fabric_resized = make_fabric_sheet(saree_fabric, (output_w, output_h), base_color)

    # 3. Build a body-aware drape mask from the real customer silhouette
    def build_body_aware_drape_mask(source_rgba: Image.Image) -> tuple[Image.Image, tuple[int, int, int, int]]:
        small_w, small_h = 192, 256
        small = source_rgba.convert("RGB").resize((small_w, small_h), Image.BILINEAR)
        px = small.load()

        # Estimate background color from corners and edge midpoints.
        sample_points = [
            (0, 0), (small_w - 1, 0), (0, small_h - 1), (small_w - 1, small_h - 1),
            (small_w // 2, 0), (small_w // 2, small_h - 1), (0, small_h // 2), (small_w - 1, small_h // 2),
        ]
        avg_r = sum(px[x, y][0] for x, y in sample_points) / len(sample_points)
        avg_g = sum(px[x, y][1] for x, y in sample_points) / len(sample_points)
        avg_b = sum(px[x, y][2] for x, y in sample_points) / len(sample_points)

        def color_distance(c):
            dr = c[0] - avg_r
            dg = c[1] - avg_g
            db = c[2] - avg_b
            return (dr * dr + dg * dg + db * db) ** 0.5

        # Flood-fill likely background from edges.
        bg = [[False for _ in range(small_w)] for _ in range(small_h)]
        q = deque()
        threshold = 55.0
        for x in range(small_w):
            q.append((x, 0))
            q.append((x, small_h - 1))
        for y in range(small_h):
            q.append((0, y))
            q.append((small_w - 1, y))

        while q:
            x, y = q.popleft()
            if x < 0 or x >= small_w or y < 0 or y >= small_h or bg[y][x]:
                continue
            if color_distance(px[x, y]) > threshold:
                continue
            bg[y][x] = True
            q.append((x + 1, y))
            q.append((x - 1, y))
            q.append((x, y + 1))
            q.append((x, y - 1))

        # Foreground mask (person).
        fg = Image.new("L", (small_w, small_h), 0)
        fg_px = fg.load()
        for y in range(small_h):
            for x in range(small_w):
                if not bg[y][x]:
                    fg_px[x, y] = 255

        # Keep only the largest connected foreground component to reduce noise.
        visited = [[False for _ in range(small_w)] for _ in range(small_h)]
        largest = []
        for sy in range(small_h):
            for sx in range(small_w):
                if visited[sy][sx] or fg_px[sx, sy] == 0:
                    continue
                comp = []
                cq = deque([(sx, sy)])
                visited[sy][sx] = True
                while cq:
                    cx, cy = cq.popleft()
                    comp.append((cx, cy))
                    for nx, ny in ((cx + 1, cy), (cx - 1, cy), (cx, cy + 1), (cx, cy - 1)):
                        if 0 <= nx < small_w and 0 <= ny < small_h and not visited[ny][nx] and fg_px[nx, ny] > 0:
                            visited[ny][nx] = True
                            cq.append((nx, ny))
                if len(comp) > len(largest):
                    largest = comp

        person = Image.new("L", (small_w, small_h), 0)
        person_px = person.load()
        for x, y in largest:
            person_px[x, y] = 255

        # Find silhouette bounds and cut off head area so face remains visible.
        bbox = person.getbbox()
        if bbox:
            _, top, _, bottom = bbox
            body_h = max(1, bottom - top)
            # If the body silhouette height is short relative to the vertical canvas,
            # it is likely an upper-body portrait or close-up shot.
            if body_h < small_h * 0.7:
                shoulder_y = top + int(body_h * 0.35)
            else:
                shoulder_y = top + int(body_h * 0.18)
            draw_local = ImageDraw.Draw(person)
            draw_local.rectangle([(0, 0), (small_w, shoulder_y)], fill=0)

        # Grow and feather mask for more natural cloth wrap.
        person = person.filter(ImageFilter.MaxFilter(size=9))
        person = person.filter(ImageFilter.GaussianBlur(radius=2.5))
        person = person.resize((output_w, output_h), Image.BILINEAR)
        raw_bbox = person.getbbox() or (210, 170, 560, 1000)
        raw_left, raw_top, raw_right, raw_bottom = raw_bbox
        mask_px = person.load()
        column_counts = []
        scan_top = raw_top + max(1, (raw_bottom - raw_top) // 3)
        for x in range(raw_left, raw_right):
            count = 0
            for y in range(scan_top, raw_bottom):
                if mask_px[x, y] > 28:
                    count += 1
            column_counts.append((x, count))

        min_count = max(8, int((raw_bottom - scan_top) * 0.08))
        strong_columns = [x for x, count in column_counts if count >= min_count]
        if strong_columns:
            full_bbox = (min(strong_columns), raw_top, max(strong_columns), raw_bottom)
        else:
            full_bbox = raw_bbox

        box_left, box_top, box_right, box_bottom = full_bbox
        box_w = box_right - box_left
        max_body_w = int(output_w * 0.46)
        if box_w > max_body_w:
            upper_top = raw_top + int((raw_bottom - raw_top) * 0.12)
            upper_bottom = raw_top + int((raw_bottom - raw_top) * 0.42)
            upper_columns = []
            for x in range(raw_left, raw_right):
                count = 0
                for y in range(upper_top, upper_bottom):
                    if mask_px[x, y] > 28:
                        count += 1
                if count >= max(6, int((upper_bottom - upper_top) * 0.08)):
                    upper_columns.append(x)

            center = (min(upper_columns) + max(upper_columns)) // 2 if upper_columns else (box_left + box_right) // 2
            box_left = max(0, center - max_body_w // 2)
            box_right = min(output_w, center + max_body_w // 2)
            full_bbox = (box_left, box_top, box_right, box_bottom)
        return person, full_bbox

    body_mask, body_bbox = build_body_aware_drape_mask(canvas_customer)
    left, top, right, bottom = body_bbox
    body_w = max(1, right - left)
    body_h = max(1, bottom - top)

    shoulder_y = top + int(body_h * 0.16)
    chest_y = top + int(body_h * 0.28)
    waist_y = top + int(body_h * 0.43)
    hip_y = top + int(body_h * 0.55)
    hem_y = min(output_h - 28, bottom + int(body_h * 0.03))
    center_x = left + body_w // 2

    drape_shape = Image.new("L", (output_w, output_h), 0)
    drape_draw = ImageDraw.Draw(drape_shape)

    # Lower saree wrap follows the detected body width from waist to feet.
    lower_points = [
        (left + int(body_w * 0.08), waist_y),
        (right - int(body_w * 0.08), waist_y),
        (right + int(body_w * 0.10), hip_y),
        (right - int(body_w * 0.02), hem_y),
        (left + int(body_w * 0.03), hem_y),
        (left - int(body_w * 0.10), hip_y),
    ]
    drape_draw.polygon(lower_points, fill=255)

    # Diagonal pallu across the torso, clipped by the real body silhouette.
    pallu_points = [
        (left + int(body_w * 0.18), shoulder_y),
        (left - int(body_w * 0.04), chest_y),
        (right - int(body_w * 0.03), waist_y + int(body_h * 0.06)),
        (right + int(body_w * 0.10), waist_y - int(body_h * 0.02)),
    ]
    drape_draw.polygon(pallu_points, fill=255)

    # Keep the face and upper neck visible, then clip cloth to the person's silhouette.
    drape_shape = ImageChops.multiply(drape_shape, body_mask)
    drape_mask_feathered = drape_shape.filter(ImageFilter.GaussianBlur(radius=5))

    blouse_shape = Image.new("L", (output_w, output_h), 0)
    blouse_draw = ImageDraw.Draw(blouse_shape)
    blouse_points = [
        (left + int(body_w * 0.14), shoulder_y),
        (right - int(body_w * 0.12), shoulder_y + int(body_h * 0.03)),
        (right - int(body_w * 0.02), waist_y),
        (left + int(body_w * 0.04), waist_y),
    ]
    blouse_draw.polygon(blouse_points, fill=210)
    blouse_shape = ImageChops.multiply(blouse_shape, body_mask).filter(ImageFilter.GaussianBlur(radius=3))

    # 4. Light & Crease Transfer (Luminosity Mapping)
    # Extract the original customer picture under the drape area
    customer_gray = canvas_customer.convert("L")
    
    # Enhance the original wrinkles/folds/highlights
    enhancer_contrast = ImageEnhance.Contrast(customer_gray)
    shading_map = enhancer_contrast.enhance(1.6) # boost crease contrast
    enhancer_bright = ImageEnhance.Brightness(shading_map)
    shading_map = enhancer_bright.enhance(0.95)   # adjust shading brightness

    # Convert back to RGBA for overlay math
    shading_rgba = shading_map.convert("RGBA")

    # 5. Combine Fabric and Folds
    # Multiply fabric by shading to apply body highlights/shadows
    draped_fabric = ImageChops.multiply(saree_fabric_resized, shading_rgba)

    # Blend slightly back with pure fabric to avoid over-darkening
    draped_fabric = Image.blend(draped_fabric, saree_fabric_resized, alpha=0.35)

    blouse_layer = Image.new("RGBA", (output_w, output_h), (*accent_color, 0))
    blouse_layer.putalpha(blouse_shape)
    blouse_layer = ImageChops.multiply(blouse_layer, shading_rgba)
    blouse_layer = Image.blend(blouse_layer, Image.new("RGBA", (output_w, output_h), (*accent_color, 255)), 0.45)
    blouse_layer.putalpha(blouse_shape)

    # 6. Apply saree-like borders along the pallu edge and hem.
    border_layer = Image.new("RGBA", (output_w, output_h), (0, 0, 0, 0))
    border_draw = ImageDraw.Draw(border_layer)
    
    border_color = (*accent_color, 245)
    border_highlight = tuple(min(255, int(c * 1.28)) for c in accent_color) + (255,)
    
    # Draw diagonal pallu border across the detected torso.
    border_draw.line(
        [(left + int(body_w * 0.14), shoulder_y), (right - int(body_w * 0.06), waist_y + int(body_h * 0.05))],
        fill=border_color,
        width=max(10, body_w // 18),
    )
    border_draw.line(
        [(left + int(body_w * 0.15), shoulder_y + 2), (right - int(body_w * 0.07), waist_y + int(body_h * 0.05) + 2)],
        fill=border_highlight,
        width=max(3, body_w // 55),
    )
    
    # Draw bottom border along the detected hemline.
    border_draw.line(
        [(left + int(body_w * 0.05), hem_y - 12), (right - int(body_w * 0.05), hem_y - 12)],
        fill=border_color,
        width=max(14, body_w // 14),
    )
    border_draw.line(
        [(left + int(body_w * 0.06), hem_y - 10), (right - int(body_w * 0.06), hem_y - 10)],
        fill=border_highlight,
        width=max(4, body_w // 45),
    )

    # Pleat hints in the lower wrap make the shape read like a saree instead of a flat dress.
    for i in range(7):
        x0 = left + int(body_w * (0.30 + i * 0.055))
        border_draw.line(
            [(x0, waist_y + int(body_h * 0.05)), (x0 - int(body_w * 0.10), hem_y - 26)],
            fill=(0, 0, 0, 55),
            width=2,
        )
    
    # Soften the borders slightly
    border_layer_blurred = border_layer.filter(ImageFilter.GaussianBlur(radius=1.5))
    border_layer_blurred.putalpha(ImageChops.multiply(border_layer_blurred.getchannel("A"), drape_shape))
    
    # Composite the border onto the draped fabric
    draped_fabric = Image.alpha_composite(draped_fabric, border_layer_blurred)

    # 7. Final Composite on Customer Image
    # Paste draped fabric onto customer canvas using the feathered drape mask
    final_composite = canvas_customer.copy()
    final_composite.alpha_composite(blouse_layer)
    final_composite.paste(draped_fabric, (0, 0), mask=drape_mask_feathered)

    # Save output
    output_filename = f"{uuid.uuid4().hex}.png"
    output_path = RESULTS_DIR / output_filename
    final_composite.convert("RGB").save(output_path, "PNG")

    print(f"[INFO] Advanced try-on complete: {output_path}")
    return str(output_path)

