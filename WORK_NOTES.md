# Work Notes — Instagram Downloader backend

## 2026-06-03 — Fix: web-converter "direct grab" never worked

**Symptom:** InstaGet returned "Instagram is rate-limiting downloads right now" for
public reels.

**Root cause (traced each source):**
- `ig_web_api` (`/api/v1/media/<id>/info/`) — now `401` from datacenter IPs (IG locked it);
  *also* `cloudscraper` crashes on a pyOpenSSL/cryptography skew (`X509_V_FLAG_NOTIFY_POLICY`)
  and the code only caught `ImportError`, so it hard-crashed instead of falling back.
- `snapsave` — down (its own infra can't reach IG: "Unable to connect to Instagram server").
- `cobalt` — now requires JWT auth.
- **`snapinsta`** — reached IG fine, but the code fed its **raw obfuscated packer** straight
  into `_parse_snapsave_html`, **skipping `_snapsave_decode`** → "could not parse".

**Fix (`app.py`):**
1. **snapinsta: decode the packer first** (`_snapsave_decode(html) or html`) → direct-grab works.
2. cloudscraper crash now caught (`except Exception`) → falls back to plain `requests`.
3. Reordered cascade to working-first: `ig_graphql → snapinsta → snapsave → ig_web_api → yt-dlp → instaloader`; fixed a snapinsta double-fetch.

**Verified:** local `ig_scrape()` returns video+thumb via snapinsta even when `ig_graphql` 401s;
live E2E on a public natgeo reel → **2.39 MB MP4**. Boots clean.
**Shipped:** Railway `sunny-creation` SUCCESS · GitHub `master` commit `6e9af8a`.

**Still open:** private/age/region-gated posts can't be fetched by any source (reported correctly).
