from flask import Flask, request, jsonify, send_file, render_template
from flask_cors import CORS
import os, uuid, json, re, glob, threading, time, shutil, subprocess, urllib.parse
import requests as req_lib
from collections import defaultdict
from html import unescape as _html_unescape

try:
    import static_ffmpeg
    static_ffmpeg.add_paths()
except Exception:
    pass

app = Flask(__name__)
CORS(app)

DOWNLOAD_DIR = '/tmp/ig_cache'
FILE_TTL     = 1800
RATE_LIMIT   = 10

os.makedirs(DOWNLOAD_DIR, exist_ok=True)

jobs        = {}
jobs_lock   = threading.Lock()
_rate_store = defaultdict(list)
_rate_lock  = threading.Lock()

_MOBILE_UA = ('Mozilla/5.0 (iPhone; CPU iPhone OS 16_6 like Mac OS X) '
              'AppleWebKit/605.1.15 (KHTML, like Gecko) Version/16.6 Mobile/15E148 Safari/604.1')
_HEADERS = {
    'User-Agent':      _MOBILE_UA,
    'Accept':          'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
    'Accept-Language': 'en-US,en;q=0.9',
    'Accept-Encoding': 'gzip, deflate, br',
    'Connection':      'keep-alive',
}


# ── Job persistence ───────────────────────────────────────────────────────────

def _job_path(job_id):
    return os.path.join(DOWNLOAD_DIR, f'job_{job_id}.json')

def _save_job(job_id, job):
    try:
        with open(_job_path(job_id), 'w') as f:
            json.dump(job, f)
    except Exception:
        pass

def _load_job_from_disk(job_id):
    try:
        with open(_job_path(job_id)) as f:
            return json.load(f)
    except Exception:
        return None

def _load_all_jobs():
    for p in glob.glob(os.path.join(DOWNLOAD_DIR, 'job_*.json')):
        try:
            with open(p) as f:
                job = json.load(f)
            job_id = os.path.basename(p)[4:-5]
            if job.get('status') in ('pending', 'processing'):
                job['status'] = 'error'
                job['error']  = 'Server restarted. Please try again.'
                _save_job(job_id, job)
            if job.get('status') == 'done' and not os.path.exists(job.get('file', '')):
                os.remove(p); continue
            jobs[job_id] = job
        except Exception:
            pass

_load_all_jobs()


# ── URL helpers ───────────────────────────────────────────────────────────────

_IG_RE = re.compile(
    r'instagram\.com/(?:p|reel|reels|tv)/([A-Za-z0-9_-]+)',
    re.IGNORECASE)

def is_valid_url(url):
    return bool(_IG_RE.search(url))

def extract_shortcode(url):
    m = _IG_RE.search(url)
    return m.group(1) if m else None

def normalize_url(url):
    url = url.strip()
    if not url.startswith('http'):
        url = 'https://' + url
    sc = extract_shortcode(url)
    return f'https://www.instagram.com/p/{sc}/' if sc else url

def make_filename(title, ext='mp4'):
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f#@]', '', title or 'instagram').strip()
    name = re.sub(r'\s+', ' ', name)
    return (name[:80] or 'instagram') + '.' + ext

def _find_ffmpeg():
    p = shutil.which('ffmpeg')
    if p: return p
    for d in ['/nix/var/nix/profiles/default/bin', '/usr/bin', '/usr/local/bin']:
        fp = os.path.join(d, 'ffmpeg')
        if os.path.isfile(fp): return fp
    nix = glob.glob('/nix/store/*/bin/ffmpeg')
    return nix[0] if nix else None


# ── Instagram via snapsave.app (same "trick" as snaptik on TikTok) + yt-dlp fallback ──

INSTA_COOKIE_FILE = os.environ.get('INSTA_COOKIE_FILE', '')
PROXY_URL = os.environ.get('PROXY_URL', '')
YTDLP_PATH = shutil.which('yt-dlp')

_SNAPSAVE_CHARSET = '0123456789abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ+/'
_SNAPSAVE_EVAL_RE = re.compile(
    r'\("([^"]+)",\s*\d+\s*,\s*"([^"]+)"\s*,\s*(\d+)\s*,\s*(\d+)\s*,\s*\d+\)\)\s*$')


def _snapsave_decode(js_body):
    """Decode snapsave.app's obfuscated eval() response into raw HTML."""
    m = _SNAPSAVE_EVAL_RE.search(js_body.strip())
    if not m:
        return None
    h, n, t, e = m.group(1), m.group(2), int(m.group(3)), int(m.group(4))
    if e <= 0 or e > len(_SNAPSAVE_CHARSET) or e >= len(n):
        return None
    sep = n[e]
    out = []
    i = 0
    L = len(h)
    while i < L:
        s = ''
        while i < L and h[i] != sep:
            s += h[i]; i += 1
        i += 1
        digits = ''.join(str(n.find(c)) for c in s if 0 <= n.find(c) < e)
        if digits:
            try:
                out.append(chr(int(digits, e) - t))
            except (ValueError, OverflowError):
                pass
    raw = ''.join(out)
    try:
        return urllib.parse.unquote(raw)
    except Exception:
        return raw


def _unescape_js_string(s):
    """Undo the JS string escapes snapsave uses inside innerHTML = \"...\"."""
    return (s.replace('\\/', '/').replace('\\"', '"')
             .replace("\\'", "'").replace('\\\\', '\\'))


def _parse_snapsave_html(decoded):
    """Extract media URLs from snapsave's decoded JS payload.

    The payload is JS that assigns HTML to innerHTML: e.g.
        document.getElementById("download-section").innerHTML = "<...>";
    Each <div class="download-items"> contains an <a href="...rapidcdn..."> (the
    download URL) and an <img src="..."> (thumb), with an icon class indicating
    image vs video."""
    if not decoded:
        return None
    if 'Unable to' in decoded or 'cannot be downloaded' in decoded.lower():
        return None
    # 1) Extract the inner HTML string from the innerHTML = "..." assignment.
    inner_m = re.search(r'innerHTML\s*=\s*"((?:\\.|[^"\\])*)"', decoded, re.DOTALL)
    html = _unescape_js_string(inner_m.group(1)) if inner_m else decoded
    # 2) Iterate every top-level "download-items" block. Use a word boundary
    #    (\b) after to avoid matching the nested "download-items__thumb" /
    #    "download-items__btn" children.
    blocks = re.findall(
        r'<div[^>]*class="[^"]*download-items\b(?!__)[^"]*"[^>]*>(.*?)'
        r'(?=<div[^>]*class="[^"]*download-items\b(?!__)|</section>|$)',
        html, re.DOTALL)
    if not blocks:
        blocks = [html]
    videos, images, thumbs = [], [], []
    for blk in blocks:
        href_m = re.search(r'<a[^>]+href="([^"]+)"', blk)
        img_m = re.search(r'<img[^>]+src="([^"]+)"', blk)
        is_video = 'icon-dlvideo' in blk
        href = _html_unescape(href_m.group(1)) if href_m else ''
        thumb = _html_unescape(img_m.group(1)) if img_m else ''
        if not href:
            continue
        if is_video:
            videos.append(href)
        else:
            images.append(href)
        if thumb:
            thumbs.append(thumb)
    # 3) Fallback: bare URL pattern anywhere in the HTML.
    if not videos and not images:
        urls = re.findall(r'https?://[^\s"\'<>]+', html)
        for u in urls:
            if 'rapidcdn.app/v2' in u or 'rapidcdn.app/video' in u:
                videos.append(u)
            elif 'rapidcdn.app' in u and 'thumb' not in u:
                images.append(u)
        thumbs = [u for u in urls if 'rapidcdn.app/thumb' in u or '.jpg' in u]
    if not videos and not images:
        return None
    # 4) Caption / title.
    cap_m = (re.search(r'<p[^>]*class="[^"]*caption[^"]*"[^>]*>([^<]+)<', html, re.I) or
             re.search(r'<h\d[^>]*>([^<]+)</h\d>', html) or
             re.search(r'alt="([^"]{8,})"', html))
    title = _html_unescape(cap_m.group(1).strip())[:120] if cap_m else 'Instagram Post'
    if title.lower().startswith('download instagram'):
        title = 'Instagram Post'
    thumb_url = thumbs[0] if thumbs else (images[0] if images else '')
    if videos:
        return {'video_url': videos[0], 'thumb_url': thumb_url, 'title': title,
                'uploader': '', 'is_video': True}
    return {'video_url': '', 'thumb_url': images[0], 'title': title,
            'uploader': '', 'is_video': False}


def _snapsave_fetch(url):
    """Primary: snapsave.app — same trick as snaptik on the TikTok site."""
    try:
        try:
            import cloudscraper
            sess = cloudscraper.create_scraper(
                browser={'browser': 'chrome', 'platform': 'windows', 'mobile': False})
        except Exception:  # cloudscraper can crash on pyOpenSSL/cryptography skew — fall back to plain requests
            sess = req_lib.Session()
            sess.headers.update({'User-Agent':
                'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
                '(KHTML, like Gecko) Chrome/124.0 Safari/537.36'})
        # Warm cookies
        sess.get('https://snapsave.app/', timeout=15)
        r = sess.post(
            'https://snapsave.app/action.php?lang=en',
            data={'url': url},
            headers={'Origin': 'https://snapsave.app',
                     'Referer': 'https://snapsave.app/',
                     'X-Requested-With': 'XMLHttpRequest',
                     'Accept': '*/*'},
            timeout=25)
        if r.status_code != 200 or not r.text:
            return None, f'snapsave HTTP {r.status_code}'
        decoded = _snapsave_decode(r.text)
        if not decoded:
            return None, 'Could not decode snapsave response.'
        if 'Unable to connect' in decoded or '"error_' in decoded:
            return None, 'snapsave could not reach Instagram for this post.'
        parsed = _parse_snapsave_html(decoded)
        if not parsed:
            return None, 'No download links found in snapsave response.'
        return parsed, None
    except Exception as e:
        return None, f'snapsave error: {e}'


def _ytdlp_fetch(url):
    if not YTDLP_PATH:
        return None, 'yt-dlp not installed'
    try:
        cmd = [YTDLP_PATH, '--dump-json', '--no-warnings', url]
        if INSTA_COOKIE_FILE and os.path.exists(INSTA_COOKIE_FILE):
            cmd += ['--cookies', INSTA_COOKIE_FILE]
        if PROXY_URL:
            cmd += ['--proxy', PROXY_URL]
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        if result.returncode == 0 and result.stdout.strip():
            data = json.loads(result.stdout)
            return {
                'video_url': data.get('url', ''),
                'thumb_url': data.get('thumbnail', ''),
                'title': data.get('title', 'Instagram Post'),
                'uploader': data.get('uploader', '') or data.get('channel', ''),
                'is_video': data.get('ext', '') in ('mp4', 'mov', 'webm'),
            }, None
        return None, (result.stderr.strip()[:200] or 'yt-dlp returned no data')
    except subprocess.TimeoutExpired:
        return None, 'yt-dlp timed out'
    except Exception as e:
        return None, f'yt-dlp error: {e}'


# ── Direct IG public endpoints + snapinsta — ported from reclip ──────────────
# Reference: /home/khaled/reclip/extractors/instagram.py
_IG_APP_ID = '936619743392459'
_IG_ASBD_ID = '129477'
_IG_ALPHA = ('ABCDEFGHIJKLMNOPQRSTUVWXYZ'
             'abcdefghijklmnopqrstuvwxyz0123456789-_')

def _shortcode_to_media_id(sc):
    n = 0
    for ch in sc:
        i = _IG_ALPHA.find(ch)
        if i < 0:
            return None
        n = n * 64 + i
    return n

def _ig_extract_from_media_obj(m):
    if not m:
        return None
    video_url = ''
    if m.get('video_versions'):
        video_url = m['video_versions'][0].get('url') or ''
    elif m.get('video_url'):
        video_url = m['video_url']
    thumb = ''
    iv = m.get('image_versions2') or {}
    if iv.get('candidates'):
        thumb = iv['candidates'][0].get('url') or ''
    elif m.get('display_url'):
        thumb = m['display_url']
    elif m.get('thumbnail_url'):
        thumb = m['thumbnail_url']
    is_video = bool(video_url) or bool(m.get('is_video'))
    user = (m.get('user') or {}).get('username') \
           or (m.get('owner') or {}).get('username') or ''
    caption_obj = m.get('caption')
    if caption_obj is None and m.get('edge_media_to_caption'):
        edges = m['edge_media_to_caption'].get('edges') or []
        caption_obj = edges[0].get('node', {}) if edges else {}
    caption = caption_obj.get('text', '') if isinstance(caption_obj, dict) else str(caption_obj or '')
    title = (caption or 'Instagram Post').strip()[:120] or 'Instagram Post'
    if not video_url and not thumb:
        return None
    try:
        duration = float(m.get('video_duration') or 0)
    except (TypeError, ValueError):
        duration = 0
    return {'video_url': video_url, 'thumb_url': thumb, 'title': title,
            'uploader': user, 'is_video': is_video, 'duration': duration}

def _ig_web_api(shortcode):
    """IG /api/v1/media/<id>/info/ — works for public posts from datacenter IPs."""
    mid = _shortcode_to_media_id(shortcode)
    if not mid:
        return None, 'Could not encode shortcode.'
    try:
        try:
            import cloudscraper
            sess = cloudscraper.create_scraper(
                browser={'browser': 'chrome', 'platform': 'windows', 'mobile': False})
        except Exception:  # cloudscraper can crash on pyOpenSSL/cryptography skew — fall back to plain requests
            sess = req_lib.Session()
        hdrs = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36',
            'Accept': 'application/json, text/plain, */*',
            'Accept-Language': 'en-US,en;q=0.9',
            'X-IG-App-ID': _IG_APP_ID,
            'X-ASBD-ID': _IG_ASBD_ID,
            'X-IG-WWW-Claim': '0',
            'X-Requested-With': 'XMLHttpRequest',
            'Origin':  'https://www.instagram.com',
            'Referer': f'https://www.instagram.com/p/{shortcode}/',
        }
        r = sess.get(f'https://www.instagram.com/api/v1/media/{mid}/info/',
                     headers=hdrs, timeout=20)
        if r.status_code != 200:
            return None, f'IG web v1 HTTP {r.status_code}'
        try:
            d = r.json()
        except Exception:
            return None, 'IG web v1: non-JSON (login wall)'
        items = d.get('items') or []
        if not items:
            return None, 'IG web v1: no items'
        info = _ig_extract_from_media_obj(items[0])
        if not info:
            return None, 'IG web v1: could not extract media'
        return info, None
    except Exception as e:
        return None, f'IG web v1 error: {e}'

def _ig_graphql(shortcode):
    """IG public GraphQL — fallback when web v1 is rate-limited."""
    try:
        sess = req_lib.Session()
        hdrs = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36',
            'Accept': '*/*',
            'X-IG-App-ID': _IG_APP_ID,
            'X-Requested-With': 'XMLHttpRequest',
            'Origin':  'https://www.instagram.com',
            'Referer': f'https://www.instagram.com/p/{shortcode}/',
        }
        r = sess.get(
            'https://www.instagram.com/graphql/query/',
            params={'doc_id': '8845758582119845',
                    'variables': '{"shortcode":"%s"}' % shortcode},
            headers=hdrs, timeout=20)
        if r.status_code != 200:
            return None, f'IG graphql HTTP {r.status_code}'
        try:
            d = r.json()
        except Exception:
            return None, 'IG graphql: non-JSON'
        m = (d.get('data') or {}).get('xdt_shortcode_media') \
            or (d.get('data') or {}).get('shortcode_media')
        info = _ig_extract_from_media_obj(m)
        if not info:
            return None, 'IG graphql: no media'
        return info, None
    except Exception as e:
        return None, f'IG graphql error: {e}'

def _snapinsta_fetch(url):
    """snapinsta.to via curl_cffi chrome124 TLS fingerprint."""
    try:
        from curl_cffi import requests as cf_req
    except ImportError:
        return None, 'curl_cffi not installed'
    try:
        sess = cf_req.Session(impersonate='chrome124')
        r = sess.get('https://snapinsta.to/en2', timeout=20)
        if r.status_code != 200:
            return None, f'snapinsta home HTTP {r.status_code}'
        r = sess.post('https://snapinsta.to/api/userverify',
                      data={'url': url},
                      headers={'X-Requested-With': 'XMLHttpRequest',
                               'Origin': 'https://snapinsta.to',
                               'Referer': 'https://snapinsta.to/en2',
                               'Accept': 'application/json, text/plain, */*'}, timeout=20)
        if r.status_code != 200:
            return None, f'snapinsta verify HTTP {r.status_code}'
        try:
            token = (r.json() or {}).get('token')
        except Exception:
            return None, 'snapinsta verify: non-JSON'
        if not token:
            return None, 'snapinsta verify: no token'
        r2 = sess.post('https://snapinsta.to/api/ajaxSearch',
                       data={'q': url, 't': 'media', 'v': '7', 'lang': 'en',
                             'cftoken': token, 'html': ''},
                       headers={'X-Requested-With': 'XMLHttpRequest',
                                'Origin': 'https://snapinsta.to',
                                'Referer': 'https://snapinsta.to/en2',
                                'Accept': '*/*'}, timeout=30)
        if r2.status_code != 200:
            return None, f'snapinsta search HTTP {r2.status_code}'
        try:
            body = r2.json()
        except Exception:
            return None, 'snapinsta search: non-JSON'
        if body.get('status') != 'ok':
            return None, body.get('mess', 'snapinsta error')
        html = body.get('data') or ''
        if not html:
            return None, body.get('mess') or 'snapinsta: empty data'
        # snapinsta returns the SAME obfuscated packer as snapsave; decode it
        # first — the raw packer has no <a>/<img> tags for the parser to find.
        decoded = _snapsave_decode(html) or html
        parsed = _parse_snapsave_html(decoded)
        if not parsed:
            return None, 'snapinsta: could not parse download HTML'
        return parsed, None
    except Exception as e:
        return None, f'snapinsta error: {e}'


def _instaloader_fetch(shortcode):
    """Last-resort: Instaloader library — public posts, no cookies."""
    try:
        import instaloader
    except ImportError:
        return None, 'instaloader not installed'
    try:
        L = instaloader.Instaloader(
            quiet=True, download_pictures=False, download_videos=False,
            download_video_thumbnails=False, download_geotags=False,
            download_comments=False, save_metadata=False, compress_json=False,
            user_agent='Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.4 Safari/605.1.15',
        )
        post = instaloader.Post.from_shortcode(L.context, shortcode)
        return {
            'video_url': post.video_url or '',
            'thumb_url': post.url or '',
            'title':     (post.caption or 'Instagram Post')[:200],
            'uploader':  post.owner_username or '',
            'is_video':  bool(post.is_video),
        }, None
    except Exception as e:
        msg = str(e)[:160] or type(e).__name__
        return None, f'instaloader: {msg}'


def ig_scrape(shortcode):
    """5-backend cascade (ported from reclip). First success wins.
    Order chosen for: speed, reliability, and likelihood-of-working from
    Railway's datacenter IP."""
    canon_p    = f'https://www.instagram.com/p/{shortcode}/'
    canon_reel = f'https://www.instagram.com/reel/{shortcode}/'
    errors = []

    def _snapinsta_either():
        # Try the reel URL first, then the /p/ form — one call each, no re-fetch.
        data, _ = _snapinsta_fetch(canon_reel)
        if data:
            return data, None
        return _snapinsta_fetch(canon_p)

    # Order = what actually works from Railway's datacenter IP first. ig_graphql
    # is the proven primary; snapinsta is the working web-converter "direct grab"
    # (decodes the snapsave-style packer). ig_web_api now mostly 401s (IG locked
    # /api/v1/.../info/) so it sits below the converters and fails fast.
    for name, fn in [
        ('ig_graphql',  lambda: _ig_graphql(shortcode)),
        ('snapinsta',   _snapinsta_either),
        ('snapsave',    lambda: _snapsave_fetch(canon_p)),
        ('ig_web_api',  lambda: _ig_web_api(shortcode)),
        ('yt-dlp',      lambda: _ytdlp_fetch(canon_p)),
        ('instaloader', lambda: _instaloader_fetch(shortcode)),
    ]:
        try:
            data, err = fn()
        except Exception as e:
            data, err = None, f'{name} crashed: {e}'
        if data and (data.get('video_url') or data.get('thumb_url')):
            return data, None
        if err:
            errors.append(f'{name}: {err}')
            print(f'[ig_scrape] {name} failed: {err}', flush=True)
    print(f'[ig_scrape] ALL backends failed: {" | ".join(errors)}', flush=True)
    joined = ' '.join(errors).lower()
    # Private / age-gated / requires login
    if 'private' in joined or 'login required' in joined or 'login or' in joined \
       or 'authentication' in joined:
        return None, ('This post appears to be private, age-gated, or login-required. '
                      'We can only download fully public Instagram posts.')
    # Rate-limited (429 from IG, 401 with "wait a few minutes", 403 Cloudflare)
    if 'http 429' in joined or 'http 401' in joined or 'http 403' in joined \
       or 'rate-limit' in joined or 'wait a few' in joined:
        return None, ('Instagram is rate-limiting downloads right now. '
                      'Try again in a few minutes, or try a different post.')
    return None, ('Instagram is currently blocking automated downloads for this post. '
                  'Try the Instagram app\'s share menu instead.')


# ── Worker ────────────────────────────────────────────────────────────────────

def _set_job(job_id, updates):
    with jobs_lock:
        jobs[job_id].update(updates)
        _save_job(job_id, jobs[job_id])

def schedule_cleanup(job_id, path):
    def _cleanup():
        time.sleep(FILE_TTL)
        try:
            if os.path.isfile(path):  os.remove(path)
            elif os.path.isdir(path): shutil.rmtree(path, ignore_errors=True)
        except Exception: pass
        try: os.remove(_job_path(job_id))
        except Exception: pass
        with jobs_lock: jobs.pop(job_id, None)
    threading.Thread(target=_cleanup, daemon=True).start()

def download_stream(src_url, output_path, job_id):
    r = req_lib.get(src_url, stream=True, timeout=120,
                    headers={**_HEADERS, 'Referer': 'https://www.instagram.com/'})
    r.raise_for_status()
    total = int(r.headers.get('content-length', 0))
    done  = 0
    with open(output_path, 'wb') as f:
        for chunk in r.iter_content(chunk_size=65536):
            if chunk:
                f.write(chunk)
                done += len(chunk)
                if total:
                    pct = min(int(done / total * 90), 90)
                    with jobs_lock:
                        if jobs.get(job_id, {}).get('status') == 'processing':
                            jobs[job_id]['progress'] = pct

def do_download(job_id, shortcode, title, fmt):
    _set_job(job_id, {'status': 'processing', 'progress': 5})
    try:
        data, err = ig_scrape(shortcode)
        if err or not data:
            _set_job(job_id, {'status': 'error', 'error': err or 'Could not fetch post.'}); return

        src_url = data['video_url'] if data['is_video'] else data['thumb_url']
        if not src_url:
            _set_job(job_id, {'status': 'error', 'error': 'No media URL found in this post.'}); return

        file_id  = str(uuid.uuid4())
        tmp_ext  = 'mp4' if data['is_video'] else 'jpg'
        tmp_path = os.path.join(DOWNLOAD_DIR, f'{file_id}.{tmp_ext}')

        download_stream(src_url, tmp_path, job_id)
        _set_job(job_id, {'progress': 92})

        if fmt == 'mp3' and data['is_video']:
            mp3_path = os.path.join(DOWNLOAD_DIR, f'{file_id}.mp3')
            ffmpeg = _find_ffmpeg()
            if ffmpeg:
                subprocess.run([ffmpeg, '-i', tmp_path, '-q:a', '0', '-map', 'a',
                                mp3_path, '-y'], capture_output=True, timeout=120)
                if os.path.exists(mp3_path):
                    os.remove(tmp_path); tmp_path = mp3_path

        t   = title or data.get('title') or f'instagram_{shortcode}'
        ext = 'mp3' if (fmt == 'mp3' and data['is_video']) else tmp_ext
        filename = make_filename(t, ext)
        _set_job(job_id, {'status': 'done', 'file': tmp_path,
                           'filename': filename, 'progress': 100})
        schedule_cleanup(job_id, tmp_path)

    except Exception:
        _set_job(job_id, {'status': 'error', 'error': 'Download failed. Please try again.'})


# ── Rate limiter ──────────────────────────────────────────────────────────────

def _check_rate(ip):
    now = time.time()
    with _rate_lock:
        _rate_store[ip] = [t for t in _rate_store[ip] if now - t < 60]
        if len(_rate_store[ip]) >= RATE_LIMIT: return False
        _rate_store[ip].append(now)
        return True

def _client_ip():
    return (request.headers.get('X-Forwarded-For', '').split(',')[0].strip()
            or request.remote_addr or 'unknown')


# ── Security headers ──────────────────────────────────────────────────────────

@app.after_request
def add_security_headers(resp):
    resp.headers['X-Frame-Options']        = 'SAMEORIGIN'
    resp.headers['X-Content-Type-Options'] = 'nosniff'
    resp.headers['Referrer-Policy']        = 'strict-origin-when-cross-origin'
    return resp


# ── Routes ────────────────────────────────────────────────────────────────────

@app.route('/')
def index():
    return render_template('index.html')

@app.route('/manifest.json')
def manifest():
    return jsonify({"name":"InstaGet","short_name":"InstaGet",
                    "description":"Download Instagram videos and photos",
                    "start_url":"/","display":"standalone",
                    "background_color":"#0a0a0a","theme_color":"#833ab4","icons":[]})

@app.route('/robots.txt')
def robots():
    return 'User-agent: *\nAllow: /\n', 200, {'Content-Type': 'text/plain'}

@app.route('/ads.txt')
def ads_txt():
    return 'google.com, pub-3956390078338144, DIRECT, f08c47fec0942fa0\n', 200, {'Content-Type': 'text/plain'}

@app.route('/health')
def health():
    # ffmpeg/yt-dlp may live at nix paths outside the worker's $PATH but
    # still be reachable via subprocess — these are informational only.
    deps = {
        'ffmpeg': bool(shutil.which('ffmpeg')),
        'yt-dlp': bool(shutil.which('yt-dlp')),
        'download_dir': os.path.isdir(DOWNLOAD_DIR),
    }
    return jsonify({'status': 'ok' if deps['download_dir'] else 'degraded',
                    'active_jobs': len(jobs),
                    'dependencies': deps}), (200 if deps['download_dir'] else 503)

# Self-unregistering service worker — defends against stale SW from old deploys
# that can intercept /download and break the blob-fetch on mobile.
@app.route('/sw.js')
def sw_js():
    return ("self.addEventListener('install',e=>self.skipWaiting());"
            "self.addEventListener('activate',e=>e.waitUntil("
            "self.registration.unregister().then(()=>self.clients.matchAll())"
            ".then(c=>c.forEach(x=>x.navigate(x.url)))));",
            200, {'Content-Type': 'application/javascript',
                  'Cache-Control': 'no-store'})

@app.route('/info', methods=['POST'])
def get_info():
    if not _check_rate(_client_ip()):
        return jsonify({'error': 'Too many requests. Please wait a moment.'}), 429
    data = request.get_json() or {}
    url  = normalize_url(data.get('url', '').strip())
    if not url or not is_valid_url(url):
        return jsonify({'error': 'Invalid Instagram URL — paste a post, Reel, or IGTV link.'}), 400
    sc = extract_shortcode(url)
    if not sc:
        return jsonify({'error': 'Could not parse Instagram URL.'}), 400
    post, err = ig_scrape(sc)
    if err or not post:
        return jsonify({'error': err or 'Could not fetch post.'}), 400
    dur = post.get('duration') or 0
    return jsonify({
        'title':        post['title'],
        'thumbnail':    post['thumb_url'],
        'uploader':     post['uploader'],
        'is_video':     post['is_video'],
        'duration':     f'{int(dur // 60)}:{int(dur % 60):02d}' if dur else '—',
        'duration_sec': dur,
        'url':          url,
    })

@app.route('/start', methods=['POST'])
def start_convert():
    if not _check_rate(_client_ip()):
        return jsonify({'error': 'Too many requests. Please wait a moment.'}), 429
    data  = request.get_json() or {}
    url   = normalize_url(data.get('url', '').strip())
    title = data.get('title', '').strip()
    fmt   = data.get('format', 'mp4')
    if fmt not in ('mp4', 'mp3'): fmt = 'mp4'
    if not is_valid_url(url):
        return jsonify({'error': 'Invalid Instagram URL'}), 400
    sc = extract_shortcode(url)
    if not sc:
        return jsonify({'error': 'Could not parse Instagram URL'}), 400

    job_id = str(uuid.uuid4())
    with jobs_lock:
        jobs[job_id] = {'status': 'pending', 'file': None, 'filename': None,
                         'error': None, 'progress': 0}
        _save_job(job_id, jobs[job_id])

    threading.Thread(target=do_download,
                     args=(job_id, sc, title or None, fmt), daemon=True).start()
    return jsonify({'job_id': job_id})

@app.route('/status/<job_id>')
def get_status(job_id):
    with jobs_lock:
        job = jobs.get(job_id)
    if not job:
        job = _load_job_from_disk(job_id)
        if job:
            with jobs_lock: jobs[job_id] = job
    if not job:
        return jsonify({'error': 'Job not found'}), 404
    return jsonify({k: job.get(k) for k in ('status', 'error', 'filename', 'progress')})

@app.route('/download/<job_id>')
@app.route('/download/<job_id>/<path:_fname>')
def download_file(job_id, _fname=None):
    with jobs_lock:
        job = jobs.get(job_id)
    if not job:
        job = _load_job_from_disk(job_id)
        if job:
            with jobs_lock: jobs[job_id] = job
    if not job or job['status'] != 'done':
        return jsonify({'error': 'File not ready — please try again.'}), 404
    path, filename = job['file'], job['filename']
    if not os.path.exists(path):
        return jsonify({'error': 'File expired. Please download again.'}), 410
    safe = re.sub(r'[^\w\s\-\.\(\)]', '', filename).strip() or 'instagram.mp4'
    mime = 'audio/mpeg' if safe.endswith('.mp3') else ('image/jpeg' if safe.endswith('.jpg') else 'video/mp4')
    return send_file(path, as_attachment=True, download_name=safe, mimetype=mime)


if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port, debug=False)
