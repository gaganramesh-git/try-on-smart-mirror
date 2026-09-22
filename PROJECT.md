# Saree Virtual Try-On — Project Document

## What This Product Does

A woman walks into a saree store. She picks up to 20 sarees she likes. Instead of physically draping each one, she stands in front of a camera, gets photographed once, and then sees herself wearing each saree on a large display screen — one at a time — by tapping through her "cart" on a tablet. No changing rooms, no re-draping, no guessing. She sees it on her own body.

---

## The Problem Being Solved

Buying a saree is slow and physically exhausting. A customer may want to compare 15–20 sarees before deciding. Each one has to be draped, assessed, and removed. Sales staff spend enormous time on this. Customers often leave without buying because the process is tiring. There is no way to quickly visualise how a saree looks on your specific body without actually wearing it.

---

## The Solution

An AI-powered virtual try-on system that:

1. Photographs the customer once (full body, standing)
2. Lets her browse the store's saree catalog on a tablet and add sarees to a cart (up to 20)
3. Sends each saree + her photo to a virtual try-on AI model
4. Displays the result on a large screen — one saree at a time
5. Updates the display instantly when she taps a different saree in her cart

The customer's face, body shape, height, and skin tone are preserved in every output. Only the saree changes.

---

## Who Uses It and How

### Roles

- **Store staff** — sets up the session, manages the saree catalog, starts/ends sessions
- **Customer** — browses sarees on the kiosk tablet, builds her cart, switches between sarees on the big display

### Complete User Flow

```
1. Staff opens the admin page on the laptop
2. Staff clicks "New Session"
3. Customer stands at the marked spot in front of the webcam
4. Staff (or customer) taps "Take Photo" on the kiosk tablet
5. A full-body photo is captured and uploaded
6. Customer browses the saree catalog on the kiosk tablet
7. She taps sarees to add them to her cart (max 20)
8. As each saree is added to cart, the system starts processing it in the background
9. Customer taps any saree in her cart → that saree's try-on result appears on the big display
10. She switches freely between sarees — the display updates each time
11. Session ends — staff clicks "End Session"
12. A QR code is shown — customer scans it on her phone to save all 20 results
```

---

## Key Features

### Must-Have (for demo)

- **Single photo capture** — webcam shot of the customer, or manual photo upload as fallback
- **Saree catalog** — staff can upload saree images (flat-lay or mannequin photos) via admin panel; each gets a name and code
- **Cart system** — customer can add up to 20 sarees; displayed as thumbnails at the bottom of the kiosk screen
- **Background processing** — as soon as a saree is added to the cart, the try-on API call fires in a background thread; results are cached
- **Real-time display sync** — tapping a saree on the kiosk instantly updates the big display via WebSocket; no page reload
- **One saree at a time on display** — the display screen shows only the currently selected saree, fullscreen, on a black background
- **Loading state** — if a result is not yet ready when tapped, the display shows a spinner; once ready, it auto-updates
- **Session management** — each customer gets a clean session; old sessions don't interfere

### Nice-to-Have (post-demo)

- QR code for customer to save results on phone
- Saree name and code shown on the display alongside the image
- Admin panel showing session history and most-selected sarees

---

## Constraints (Non-Negotiable)

| Constraint | Detail |
|---|---|
| Budget | Zero — all APIs and services must be completely free |
| Developer | One person, second-year engineering student, working alone |
| Timeline | 8 weeks to demo-ready |
| Hardware (now) | Demo runs entirely on one laptop — no TV, no tablet, no webcam required for demo |
| Hardware (later) | When budget exists: laptop + TV (HDMI) + Android tablet + webcam |
| Internet | Required only for try-on API calls; app must work on localhost otherwise |

---

## Demo Setup (Laptop Only)

Since there is no hardware budget right now, the demo is simulated on a single laptop:

- **Left browser window** → `localhost:5000/kiosk` — simulates the tablet the customer uses
- **Right browser window** → `localhost:5000/display` — simulates the big TV screen (press F11 to fullscreen)
- A photo is uploaded manually from disk instead of using a webcam
- Both windows stay in sync via WebSocket running on localhost

This is a fully working demo. Everything that works here will work identically on real hardware later — no code changes needed.

---

## What "Done" Looks Like for the Demo

The demo is successful when a reviewer can watch the following happen live without explanation:

1. A photo of a woman is uploaded
2. Five or more saree images are in the catalog
3. Reviewer adds 3 sarees to the cart on the kiosk window
4. The try-on results appear (or are already cached)
5. Reviewer taps Saree 1 → display window shows her in Saree 1
6. Reviewer taps Saree 2 → display window switches to Saree 2 instantly
7. Reviewer taps Saree 3 → display switches again
8. The woman's face and body are recognisably the same in all three

If that sequence works cleanly, the demo is a success.

---

## What This Is Not

- This is **not** a mobile app
- This is **not** a real-time augmented reality mirror
- This is **not** training any AI model — it calls an existing model via API
- This is **not** a 3D simulation — outputs are 2D photorealistic images
- This is **not** cloud-hosted — it runs locally on one machine

---

## Future Direction (After Demo)

Once funded:

- Replace free HuggingFace API with paid fashn.ai for faster, more reliable results
- Deploy on real hardware: laptop + 55" TV + Android tablet + Logitech webcam
- Build a store admin dashboard with analytics
- Add QR-code share feature for customers
- Expand to lehengas and salwar suits
- License to stores on a SaaS model (₹15,000–20,000/month per store)
