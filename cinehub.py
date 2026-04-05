"""
╔═══════════════════════════════════════════════════════╗
║   CineHub — Stage.in Download Bot                     ║
║   Automated HLS video scraper & downloader            ║
║   rclone Drive upload • Audio extraction              ║
╚═══════════════════════════════════════════════════════╝

Author:  CineHub Bot
Version: 3.1.0
License: Personal use only

Features:
  • Scrapes m3u8 from Stage.in using headless Chromium
  • Persistent login via saved browser session
  • Quality selection (4K / 1080p / 720p / 480p / 360p / Audio-only)
  • Segment-by-segment download with signed token forwarding
  • Auto-merges video + audio with ffmpeg
  • Audio-only extraction (AAC/M4A)
  • Google Drive upload via rclone (unlimited size)
  • Progress updates in Telegram chat

Setup:
  1. pip install python-telegram-bot playwright
  2. playwright install chromium
  3. rclone config  (create a remote named 'drive')
  4. Create a .env file with your CINEHUB_TOKEN and OWNER_ID
  5. python cinehub.py
  6. Send /login to authenticate with Stage.in
"""
import asyncio
import json
import os
import re
import shutil
import subprocess
import time
import logging
from concurrent.futures import ThreadPoolExecutor, as_completed
from urllib.parse import urlparse
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, BotCommand
from telegram.ext import (
    Application, CommandHandler, MessageHandler, CallbackQueryHandler,
    ConversationHandler, filters, ContextTypes
)
from playwright.async_api import async_playwright
import httpx
from dotenv import load_dotenv

# Load configuration from .env file
load_dotenv()

# ============================================================
#  CONFIG & USER AUTH
# ============================================================
BOT_TOKEN = os.environ.get("CINEHUB_TOKEN")
if not BOT_TOKEN:
    raise ValueError("❌ CINEHUB_TOKEN environment variable not set. Please set it before running. Example: set CINEHUB_TOKEN=your_token_here")

OWNER_ID = int(os.environ.get("OWNER_ID", "6940979626"))  # Admin control
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DOWNLOAD_DIR = os.path.join(BASE_DIR, "downloads")
SESSION_DIR = os.path.join(BASE_DIR, "sessions")
os.makedirs(SESSION_DIR, exist_ok=True)
USERS_FILE = os.path.join(BASE_DIR, "users.json")
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
HEADERS = {"User-Agent": UA}

PLATFORMS = {
    "stage": ("stage.in", "session_stage.json")
}

def load_users():
    if os.path.exists(USERS_FILE):
        data = json.load(open(USERS_FILE))
        # MIGRATION: Convert list of premium IDs to dict with default expiry
        if isinstance(data.get("premium"), list):
            new_premium = {}
            for uid in data["premium"]:
                # Default to 30 days from now for existing premium users
                new_premium[str(uid)] = time.time() + (30 * 86400)
            data["premium"] = new_premium
            save_users(data)
        return data
    return {"authorized": [], "premium": {}}

def save_users(data):
    with open(USERS_FILE, "w") as f:
        json.dump(data, f, indent=2)

# --- ADMIN LIST ---
ADMIN_IDS = [OWNER_ID, 6796307271]

def is_authorized(uid):
    if uid in ADMIN_IDS: return True
    users = load_users()
    return uid in users["authorized"] or uid in users["premium"]

def is_premium(uid):
    if uid in ADMIN_IDS: return True
    users = load_users()
    s_uid = str(uid)
    if s_uid in users["premium"]:
        expiry = users["premium"][s_uid]
        if time.time() < expiry:
            return True
        else:
            # Auto-demote expired user
            users["premium"].pop(s_uid)
            if uid not in users["authorized"]:
                users["authorized"].append(uid)
            save_users(users)
            log.info(f"Subscription expired for {uid}")
    return False

# rclone config
RCLONE_BIN = os.path.join(BASE_DIR, "rclone.exe") if os.name == "nt" else "rclone"
RCLONE_REMOTE = os.environ.get("RCLONE_REMOTE", "drive")       # rclone remote name
RCLONE_FOLDER = os.environ.get("RCLONE_FOLDER", "CineHub")    # folder on Drive

os.makedirs(DOWNLOAD_DIR, exist_ok=True)
logging.basicConfig(format="%(asctime)s [%(levelname)s] %(message)s", level=logging.INFO)
log = logging.getLogger("cinehub")

# Conversation states
LOGIN_PHONE, LOGIN_OTP = range(2)

QUALITIES = {
    "🎬 4K (2160p)":  0,
    "📺 1080p (Full HD)": 2,
    "📱 720p (HD)":   4,
    "💾 480p (SD)":   5,
    "⚡ 360p (Fast)": 6,
    "🎵 Audio Only":  99,
}

# ============================================================
#  HELPERS
# ============================================================

async def fetch(url: str, client: httpx.AsyncClient) -> bytes:
    try:
        r = await client.get(url, headers=HEADERS, timeout=30)
        r.raise_for_status()
        return r.content
    except Exception as e:
        log.error(f"fetch error: {e} | URL: {url}")
        raise

async def fetch_text(url: str, client: httpx.AsyncClient) -> str:
    data = await fetch(url, client)
    return data.decode("utf-8")

def base_of(url: str) -> str:
    p = urlparse(url)
    return f"{p.scheme}://{p.netloc}{p.path.rsplit('/', 1)[0]}"

def qs_of(url: str) -> str:
    return urlparse(url).query

def absolute(rel: str, base: str, qs: str) -> str:
    if rel.startswith("http"):
        return rel if "?" in rel or not qs else f"{rel}?{qs}"
    full = f"{base}/{rel}"
    return f"{full}?{qs}" if qs else full

def progress_bar(pct: float, width: int = 20) -> str:
    """Generate a sleek progress bar with emoji blocks."""
    pct = max(0, min(100, pct))
    filled = int(width * pct / 100)
    empty = width - filled
    bar = "█" * filled + "░" * empty
    return f"`{bar}` *{pct:.0f}%*"

# ============================================================
#  SESSION
# ============================================================

VALID_COOKIE_FIELDS = {"name", "value", "domain", "path", "expires", "httpOnly", "secure", "sameSite"}

def parse_netscape_cookies(content: str) -> list:
    """Parse Netscape HTTP Cookie File format (.txt)."""
    cookies = []
    lines = content.split("\n")
    for line in lines:
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split("\t")
        if len(parts) >= 7:
            # Domain, Subdomain, Path, Secure, Expire, Name, Value
            cookie = {
                "domain": parts[0],
                "path": parts[2],
                "secure": parts[3].upper() == "TRUE",
                "expires": int(parts[4]) if parts[4].isdigit() else -1,
                "name": parts[5],
                "value": parts[6],
                "httpOnly": False # Netscape format doesn't explicitly flag this easily
            }
            cookies.append(cookie)
    return cookies

def detect_platform_from_cookies(cookies: list) -> str | None:
    """Detect if cookies are for Stage.in."""
    for c in cookies:
        domain = c.get("domain", "").lower()
        if "stage.in" in domain:
            return "stage"
    return None

def convert_to_storage_state(cookies: list) -> dict:
    """Convert a flat list of cookies to Playwright storage_state format."""
    # Sanitize cookies: keep only fields Chromium accepts
    clean = []
    for c in cookies:
        cookie = {k: v for k, v in c.items() if k in VALID_COOKIE_FIELDS}
        if "name" in cookie and "value" in cookie and "domain" in cookie:
            # sameSite must be Lax, Strict, or None
            if "sameSite" in cookie and cookie["sameSite"] not in ("Strict", "Lax", "None"):
                cookie["sameSite"] = "Lax"
            # expires -1 is session cookie, Playwright likes it removed or 0
            if "expires" in cookie and cookie["expires"] <= 0:
                cookie.pop("expires")
            clean.append(cookie)
    return {"cookies": clean, "origins": []}


def get_session_path(platform: str) -> str:
    return os.path.join(SESSION_DIR, f"session_{platform}.json")

def save_session(state: dict, platform: str = "stage"):
    with open(get_session_path(platform), "w") as f:
        json.dump(state, f)

def load_session(platform: str = "stage") -> dict | None:
    path = get_session_path(platform)
    if not os.path.exists(path):
        return None
    with open(path) as f:
        data = json.load(f)
    
    # Bug fix: if the session was saved as a flat list of cookies, wrap it in a dict
    if isinstance(data, list):
        data = convert_to_storage_state(data)
        
    # Sanitize cookies: keep only fields Chromium accepts
    if "cookies" in data:
        clean = []
        for c in data["cookies"]:
            cookie = {k: v for k, v in c.items() if k in VALID_COOKIE_FIELDS}
            if "name" in cookie and "value" in cookie and "domain" in cookie:
                if "sameSite" in cookie and cookie["sameSite"] not in ("Strict", "Lax", "None"):
                    cookie["sameSite"] = "Lax"
                if "expires" in cookie and cookie["expires"] == -1:
                    cookie.pop("expires")
                clean.append(cookie)
        data["cookies"] = clean
    return data

def has_session(platform: str = "stage") -> bool:
    return os.path.exists(get_session_path(platform))

# ============================================================
#  SCRAPER — headless Chromium, fast m3u8 capture
# ============================================================

# Resolution -> label map
RES_LABELS = {
    "3840x2160": "2160p", "2560x1440": "1440p", "1920x1080": "1080p",
    "1280x720": "720p", "854x480": "480p", "640x360": "360p", "426x240": "240p",
}

def res_to_label(res: str) -> str:
    """Convert '1920x1080' to '1080p'."""
    if res in RES_LABELS:
        return RES_LABELS[res]
    m = re.match(r"\d+x(\d+)", res)
    return f"{m.group(1)}p" if m else res


async def scrape_stage(url: str) -> tuple[str | None, dict | None, str | None]:
    """Returns (m3u8_url, metadata_dict, error_string) for Stage.in."""
    session = load_session("stage")
    if not session:
        return None, None, "No session. Use /login or import cookies first."

    BLOCK_TYPES = {"image", "stylesheet", "font"}
    master = None
    meta = {"title": "Video", "year": "", "lang": "", "lang_full": "", "poster": "", "description": "", "info": "", "platform": "STAGE"}

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        try:
            ctx = await browser.new_context(storage_state=session, user_agent=UA,
                                       viewport={"width": 1280, "height": 720})
            
            page = await ctx.new_page()
            async def handle_route(route):
                if route.request.resource_type in BLOCK_TYPES:
                    await route.abort()
                else:
                    await route.continue_()
            await page.route("**/*", handle_route)

            def on_req(req):
                nonlocal master
                if "playlist.m3u8" in req.url and master is None:
                    master = req.url

            page.on("request", on_req)
            
            try:
                await page.goto(url, wait_until="domcontentloaded", timeout=20_000)
                for _ in range(250): 
                    if master: break
                    await asyncio.sleep(0.1)
            except Exception as e:
                log.warning(f"stage nav: {e}")

            try:
                metadata = await page.evaluate("""() => {
                    const getVal = (sel) => document.querySelector(sel)?.innerText?.strip() || '';
                    const getAttr = (sel, attr) => document.querySelector(sel)?.getAttribute(attr) || '';
                    return {
                        title: getVal('h1') || getVal('[class*="title"]') || getAttr('meta[property="og:title"]', 'content') || document.title,
                        description: getVal('p[class*="description"], [class*="synopsis"]') || getAttr('meta[name="description"]', 'content'),
                        info: getVal('[class*="metadata"], [class*="movie-info"]'),
                        poster: getAttr('img[class*="poster"], img[class*="thumbnail"], img[class*="banner"]', 'src') || getAttr('meta[property="og:image"]', 'content'),
                        body_text: document.body.innerText.slice(0, 2000)
                    };
                }""")
                
                raw_title = metadata["title"]
                # Clean title: remove "Watch", "Online", etc from start
                raw_title = re.sub(r'^(Watch|Online|Free|Download)\s+', '', raw_title, flags=re.I).strip()
                # Clean title: remove platform suffix
                raw_title = re.sub(r'\s*[-|]\s*(Stage|Watch|Online|Free|HD).*', '', raw_title, flags=re.I).strip()
                meta["title"] = raw_title
                
                y_match = re.search(r'\((20\d\d)\)', raw_title)
                if y_match:
                    meta["year"] = y_match.group(1)
                else:
                    y_match = re.search(r'\b(20[12]\d)\b', metadata["body_text"])
                    if y_match:
                        y = int(y_match.group(1))
                        if 2010 <= y <= 2026: meta["year"] = str(y)
                
                meta["description"] = metadata["description"][:300] + "..." if len(metadata["description"]) > 300 else metadata["description"]
                meta["info"] = metadata["info"].replace("\n", " • ")
                meta["poster"] = metadata["poster"] if metadata["poster"].startswith('http') else ""
            except: pass

            lang_map = {
                "marathi": ("Marathi", "Mar"), "hindi": ("Hindi", "Hin"),
                "haryanvi": ("Haryanvi", "Har"), "rajasthani": ("Rajasthani", "Raj"),
                "gujarati": ("Gujarati", "Guj"), "punjabi": ("Punjabi", "Pun"),
                "tamil": ("Tamil", "Tam"), "telugu": ("Telugu", "Tel"),
                "kannada": ("Kannada", "Kan"), "bengali": ("Bengali", "Ben"),
                "malayalam": ("Malayalam", "Mal"), "bhojpuri": ("Bhojpuri", "Bho"),
            }
            for key, (full, short) in lang_map.items():
                if key in url.lower():
                    meta["lang_full"], meta["lang"] = full, short
                    break

            try:
                if master:
                    save_session(await ctx.storage_state(), "stage")
            except: pass
            
        finally:
            await browser.close()

    if master:
        return master, meta, None
    return None, None, "m3u8 not found. Session expired? /login again."

# Global executor for background tasks (ffmpeg, rclone, write)
EXECUTOR = ThreadPoolExecutor(max_workers=8)
DL_WORKERS = 10  # concurrent segment downloads

async def download_segments(m3u8_url: str, fallback_qs: str, out: str, cb=None) -> bool:
    """Async downloader for HLS segments using httpx."""
    async with httpx.AsyncClient(headers=HEADERS, timeout=60) as client:
        base = base_of(m3u8_url)
        qs = qs_of(m3u8_url) or fallback_qs
        content = await fetch_text(m3u8_url, client)

        init_url = None
        segs = []
        for line in content.strip().split("\n"):
            line = line.strip()
            if line.startswith("#EXT-X-MAP"):
                m = re.search(r'URI="([^"]+)"', line)
                if m: init_url = absolute(m.group(1), base, qs)
            elif line and not line.startswith("#"):
                segs.append(absolute(line, base, qs))

        total = len(segs)
        semaphore = asyncio.Semaphore(DL_WORKERS)
        
        # Buffer for ordered writing
        results = [None] * total
        done = 0
        total_bytes = 0
        async def dl_one(idx, url):
            nonlocal done, total_bytes
            async with semaphore:
                for attempt in range(3):
                    try:
                        r = await client.get(url, timeout=30)
                        r.raise_for_status()
                        results[idx] = r.content
                        done += 1
                        total_bytes += len(r.content)
                        if cb and (done % 50 == 0 or done == total):
                            await cb(done / total * 100, total_bytes / 1048576)
                        return
                    except:
                        await asyncio.sleep(0.5 * (attempt + 1))
                results[idx] = b"" # Failed

        tasks = [asyncio.create_task(dl_one(i, url)) for i, url in enumerate(segs)]
        
        # Write init map if present
        if init_url:
            init_data = await fetch(init_url, client)
            with open(out, "wb") as f:
                f.write(init_data)
        else:
            open(out, "wb").close()

        # Download all and then write in order
        await asyncio.gather(*tasks)
        
        # Sync write to disk
        def write_all():
            with open(out, "ab") as f:
                for chunk in results:
                    if chunk: f.write(chunk)
        
        await asyncio.get_event_loop().run_in_executor(EXECUTOR, write_all)
        return os.path.getsize(out) > 0


def detect_codec(stream_info: str) -> str:
    """Detect video codec from CODECS attribute."""
    m = re.search(r'CODECS="([^"]+)"', stream_info)
    if m:
        codecs = m.group(1)
        if "hvc1" in codecs or "hev1" in codecs:
            return "H265"
        if "avc1" in codecs:
            return "H264"
        if "av01" in codecs:
            return "AV1"
    return "H265"  # Stage.in defaults to H265


def build_filename(meta: dict, res_label: str, codec: str) -> str:
    """Build scene-style filename."""
    title = meta.get("title", "Video").strip()
    year = meta.get("year", "")
    lang = meta.get("lang_full", "")

    # Sanitize title
    title = re.sub(r'[\\/:*?"<>|]', '', title)
    title = title.strip('. ')
    if not title:
        title = "Video"

    parts = [title]
    if year:
        parts[0] = f"{title} ({year})"
    parts.append(res_label)
    parts.append(f"{meta.get('platform', 'WEB')} WEB-DL")
    if lang:
        parts.append(f"{lang} AAC 2.0")
    else:
        parts.append("AAC 2.0")
    parts.append(codec)
    parts.append("~ CineHub")

    return " ".join(parts)


async def parse_playlist_streams(master_url: str, client: httpx.AsyncClient):
    """Parse master m3u8 to find streams and audio tracks."""
    content = await fetch_text(master_url, client)
    qs = qs_of(master_url)
    base = base_of(master_url)

    streams, audio_url = [], None
    lines = content.strip().split("\n")
    for i, line in enumerate(lines):
        line = line.strip()
        if "TYPE=AUDIO" in line:
            m = re.search(r'URI="([^"]+)"', line)
            if m:
                audio_url = absolute(m.group(1), base, qs)
        elif line.startswith("#EXT-X-STREAM-INF"):
            bw = int(m.group(1)) if (m := re.search(r"BANDWIDTH=(\d+)", line)) else 0
            res = m.group(1) if (m := re.search(r"RESOLUTION=(\d+x\d+)", line)) else "?"
            if i + 1 < len(lines) and not lines[i + 1].strip().startswith("#"):
                streams.append({
                    "url": absolute(lines[i + 1].strip(), base, qs),
                    "bw": bw, "res": res, "info": line
                })

    if streams:
        streams.sort(key=lambda s: s["bw"], reverse=True)
    return streams, audio_url, qs


async def download_hls(master_url: str, name: str, qi: int = 0, cb=None, meta: dict = None):
    async with httpx.AsyncClient(headers=HEADERS, timeout=60) as client:
        streams, audio_url, qs = await parse_playlist_streams(master_url, client)
        if not streams:
            return None

        sel = streams[min(qi, len(streams) - 1)]
        res_label = res_to_label(sel["res"])
        codec = detect_codec(sel["info"])

        # Build proper filename
        if meta:
            filename = build_filename(meta, res_label, codec)
        else:
            platform = meta.get("platform", "WEB") if meta else "WEB"
            filename = f"{name} {res_label} {platform} WEB-DL {codec} ~ CineHub"

        if cb:
            await cb("info", f"📁 `{filename}`\n📊 {sel['res']} ({res_label}) • {codec}")

        tmp = os.path.join(DOWNLOAD_DIR, f".{name}_tmp")
        os.makedirs(tmp, exist_ok=True)

        vf = os.path.join(tmp, "v.mp4")
        
        async def v_cb(p, s):
            if cb: await cb("dl", f"📁 `{filename}`\n\n📥 *Video*\n{progress_bar(p)}\n💾 {s:.0f} MB")
            
        await download_segments(sel["url"], qs, vf, v_cb)

        af = None
        if audio_url:
            af = os.path.join(tmp, "a.m4a")
            async def a_cb(p, s):
                if cb: await cb("dl", f"📁 `{filename}`\n\n✅ Video done\n\n🎵 *Audio*\n{progress_bar(p)}\n💾 {s:.0f} MB")
            await download_segments(audio_url, qs, af, a_cb)

        out = os.path.join(DOWNLOAD_DIR, f"{filename}.mkv")
        
        def run_ffmpeg():
            cmd = ["ffmpeg", "-y", "-i", vf]
            if af and os.path.exists(af):
                cmd += ["-i", af]
            cmd += ["-c", "copy", out]
            subprocess.run(cmd, capture_output=True)
        
        await asyncio.get_event_loop().run_in_executor(EXECUTOR, run_ffmpeg)

        shutil.rmtree(tmp, ignore_errors=True)
        return (out, filename, sel["res"], os.path.getsize(out)) if os.path.exists(out) else None


async def download_audio_only(master_url: str, name: str, cb=None, meta: dict = None):
    """Async download only audio track from HLS stream."""
    async with httpx.AsyncClient(headers=HEADERS, timeout=60) as client:
        qs = qs_of(master_url)
        base = base_of(master_url)
        content = await fetch_text(master_url, client)

        audio_url = None
        lines = content.strip().split("\n")
        for line in lines:
            line = line.strip()
            if "TYPE=AUDIO" in line:
                m = re.search(r'URI="([^"]+)"', line)
                if m:
                    audio_url = absolute(m.group(1), base, qs)
                    break

        if not audio_url:
            return None

        # Build audio filename
        title = "Audio"
        if meta:
            title = meta.get("title", "Audio").strip()
            title = re.sub(r'[\\/:*?"<>|]', '', title).strip('. ') or "Audio"
            year = meta.get("year", "")
            lang = meta.get("lang_full", "")
            parts = [title]
            if year:
                parts[0] = f"{title} ({year})"
            platform = meta.get("platform", "WEB") if meta else "WEB"
            parts.append(f"{platform} WEB-DL")
            if lang:
                parts.append(lang)
            parts.append("AAC 2.0 ~ CineHub")
            filename = " ".join(parts)
        else:
            filename = f"{name} WEB-DL AAC 2.0 ~ CineHub"

        if cb:
            await cb("info", f"📁 `{filename}`\n🎵 Audio-only mode")

        tmp = os.path.join(DOWNLOAD_DIR, f".{name}_audio_tmp")
        os.makedirs(tmp, exist_ok=True)

        af = os.path.join(tmp, "audio.m4a")
        async def a_cb(p, s):
            if cb: await cb("dl", f"📁 `{filename}`\n\n🎵 *Audio*\n{progress_bar(p)}\n💾 {s:.0f} MB")
        await download_segments(audio_url, qs, af, a_cb)

        out = os.path.join(DOWNLOAD_DIR, f"{filename}.m4a")
        await asyncio.get_event_loop().run_in_executor(EXECUTOR, lambda: shutil.move(af, out))
        shutil.rmtree(tmp, ignore_errors=True)
        return (out, filename, "Audio", os.path.getsize(out)) if os.path.exists(out) else None


# ============================================================
#  RCLONE — Google Drive upload via rclone
# ============================================================

def rclone_is_ready() -> bool:
    """Check if rclone is installed and has the configured remote."""
    try:
        result = subprocess.run(
            [RCLONE_BIN, "listremotes"],
            capture_output=True, text=True, timeout=10
        )
        remotes = result.stdout.strip().split("\n")
        return f"{RCLONE_REMOTE}:" in remotes
    except Exception:
        return False


async def rclone_upload(path: str, filename: str, progress_cb=None) -> str | None:
    """Upload file to Google Drive via rclone. Returns shareable link or None."""
    dest = f"{RCLONE_REMOTE}:{RCLONE_FOLDER}/{filename}"
    file_size = os.path.getsize(path)
    file_mb = file_size / 1048576
    log.info(f"rclone: uploading {filename} ({file_mb:.0f} MB) -> {dest}")

    try:
        proc = await asyncio.create_subprocess_exec(
            RCLONE_BIN, "copyto", path, dest,
            "--progress", "--stats", "1s", "--stats-one-line",
            "-v",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )

        # rclone writes progress with \r (carriage return), not \n
        # Read raw chunks and parse percentage from them
        last_pct = -1
        buffer = b""
        while True:
            chunk = await proc.stderr.read(512)
            if not chunk:
                break
            buffer += chunk
            # Split on \r or \n to get lines
            parts = re.split(rb'[\r\n]+', buffer)
            buffer = parts[-1]  # keep incomplete last part
            for part in parts[:-1]:
                text = part.decode("utf-8", errors="ignore").strip()
                if not text:
                    continue
                # Match: "Transferred: 50 MiB / 167 MiB, 30%, 5.2 MiB/s"
                m = re.search(r'(\d+)%', text)
                if m and progress_cb:
                    pct = int(m.group(1))
                    if pct != last_pct and pct - last_pct >= 5:
                        last_pct = pct
                        # Extract speed if available
                        speed = ""
                        sm = re.search(r'(\d+\.?\d*\s*[KMG]i?B/s)', text)
                        if sm:
                            speed = f" • {sm.group(1)}"
                        await progress_cb(pct, speed)

        await proc.wait()

        if proc.returncode != 0:
            log.error(f"rclone failed with exit code {proc.returncode}")
            return None

        # Get shareable link
        link_proc = await asyncio.create_subprocess_exec(
            RCLONE_BIN, "link", dest,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, _ = await link_proc.communicate()
        link = stdout.decode().strip()

        if link and link.startswith("http"):
            log.info(f"rclone upload complete: {filename} -> {link}")
            return link
        else:
            log.info(f"rclone upload complete: {filename} (no link generated)")
            return f"Uploaded to {RCLONE_REMOTE}:/{RCLONE_FOLDER}/{filename}"

    except Exception as e:
        log.error(f"rclone upload failed: {e}")
        return None


# ============================================================
#  TELEGRAM HANDLERS
# ============================================================

#  TELEGRAM HANDLERS & CMDS
# ============================================================

def auth(uid):
    return is_authorized(uid)

async def cmd_start(u: Update, c: ContextTypes.DEFAULT_TYPE):
    welcome_text = (
        "🌟 *Stage.in Premium Downloader* 🌟\n"
        "Welcome to the ultimate entertainment hub! 🎬\n\n"
        "I am your dedicated assistant for high-speed, high-definition regional content from *Stage.in*. Experience your favorite stories like never before. 👑\n\n"
        "💎 *Why Choose Us:*\n"
        "📺 *Cinematic Visuals:* Unlock breathtaking 1080p and 4K resolutions.\n"
        "☁️ *Cloud Convenience:* Your files delivered instantly to Google Drive.\n"
        "🎵 *Studio Sound:* Extract pure Audio Tracks for your personal playlist.\n"
        "⚡ *Zero Friction:* No ads, no waiting, just Lightning-Fast Processing.\n\n"
        "🚀 *How to use:*\n"
        "1️⃣ **Copy** a movie or web-series link strictly from Stage.in. 🔗\n"
        "2️⃣ **Paste** it right here in this chat. 📥\n"
        "3️⃣ **Select** your quality and let me handle the rest! ✅\n\n"
        "✨ *Ready for the VIP Treatment? Type /premium to unlock all features!* 💎"
    )
    
    kb = [
        [InlineKeyboardButton("📢 Join Updates Channel", url="https://t.me/CineHub_Rips")],
        [InlineKeyboardButton("💎 Get Premium Access", callback_data="q_premium_info")]
    ]
    # Note: Callback q_premium_info isn't explicitly handled yet, but I'll add it or just use the button to show text.
    # Actually, better to just use URL or tell them to type /premium.
    # Let's just use the channel button as requested.
    
    reply_markup = InlineKeyboardMarkup(kb)
    await u.message.reply_text(welcome_text, reply_markup=reply_markup, parse_mode="Markdown")

async def cmd_help(u: Update, c: ContextTypes.DEFAULT_TYPE):
    help_text = (
        "🌟 *How to use Stage.in Downloader* 🌟\n\n"
        "Getting high-quality regional content is simple. Just follow these steps:\n\n"
        "1️⃣ **Copy** a link from the Stage.in app or website. 🔗\n"
        "2️⃣ **Paste** it directly into this chat. 📥\n"
        "3️⃣ **Pick** your desired video quality, or choose Audio-Only! ✅\n\n"
        "The bot will securely upload the file to Google Drive and provide a fast, direct download link.\n\n"
        "💎 *Tap /premium to view our exclusive plans!*"
    )
    
    kb = [[InlineKeyboardButton("📢 Join Updates Channel", url="https://t.me/CineHub_Rips")]]
    reply_markup = InlineKeyboardMarkup(kb)
    await u.message.reply_text(help_text, reply_markup=reply_markup, parse_mode="Markdown")

async def cmd_premium(u: Update, c: ContextTypes.DEFAULT_TYPE):
    await u.message.reply_text(
        "━━━━━━━━━━━━━━━━━━━━━━━━\n"
        " 🥳 *PREMIUM SUBSCRIPTION* 🥳\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
        "🌟 *Choose the plan that's right for you* ⚡️\n\n"
        "╭───🔅 *ALL PLAN DETAILS* 🔅───╮\n"
        "│\n"
        "│      🎁 *Cheapest Price* { 🎉 }\n"
        "│\n"
        "╰─▸    *₹59/-*    (for 1 Month/-)\n"
        "│\n"
        "╰─▸    *₹119/-*    (for 3 Months/-)\n"
        "│\n"
        "╰─▸    *₹349/-*   (for 6 Months/-)\n"
        "│\n"
        "╰─▸    *₹699/-*   (for 1 Year/-)\n"
        "╰──────────────────────╯\n\n"
        "╭────🔅 *BENEFITS* 🔅───────╮\n"
        "│\n"
        "╰─▸ *Get All Latest Movies & Series* 🎬\n"
        "╰─▸ *Unlock 1080p & 4K Quality* 🔥\n"
        "╰─▸ *Unlock Audio-only Extraction* 🎵\n"
        "╰──────────────────────╯\n\n"
        "       ✆ *Admin*🕵️ 👉 [RajGor_Paras](https://t.me/RajGor_Paras)\n\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━\n"
        "_Note: Latest Movies available only._",
        parse_mode="Markdown",
        disable_web_page_preview=True
    )

async def cmd_me(u: Update, c: ContextTypes.DEFAULT_TYPE):
    uid = u.effective_user.id
    users = load_users()
    s_uid = str(uid)
    text = f"🆔 *Your ID:* `{uid}`\n"
    if uid == OWNER_ID:
        text += "👑 *Rank:* `Owner/Admin`"
    elif s_uid in users["premium"]:
        expiry = users["premium"][s_uid]
        days_left = int((expiry - time.time()) / 86400)
        date_str = time.strftime('%d-%m-%Y', time.localtime(expiry))
        text += f"💎 *Rank:* `Premium Member`\n📅 *Expiry:* `{date_str}` (*{days_left}d*)"
    elif uid in users["authorized"]:
        text += "👤 *Rank:* `Authorized Member`"
    else:
        text += "🔒 *Rank:* `Un-authorized`"
    
    await u.message.reply_text(text, parse_mode="Markdown")

# --- ADMIN COMMANDS ---

async def cmd_auth(u: Update, c: ContextTypes.DEFAULT_TYPE):
    if u.effective_user.id != OWNER_ID: return
    if not c.args:
        await u.message.reply_text("Usage: `/auth <user_id>`")
        return
    uid = int(c.args[0])
    users = load_users()
    if uid not in users["authorized"]:
        users["authorized"].append(uid)
        save_users(users)
    await u.message.reply_text(f"✅ User `{uid}` authorized as Member.")

async def cmd_unauth(u: Update, c: ContextTypes.DEFAULT_TYPE):
    if u.effective_user.id != OWNER_ID: return
    if not c.args: return
    uid = int(c.args[0])
    users = load_users()
    if uid in users["authorized"]: users["authorized"].remove(uid)
    if uid in users["premium"]: users["premium"].remove(uid)
    save_users(users)
    await u.message.reply_text(f"❌ User `{uid}` access revoked.")

async def cmd_add_premium(u: Update, c: ContextTypes.DEFAULT_TYPE):
    if u.effective_user.id != OWNER_ID: return
    if not c.args:
        await u.message.reply_text("Usage: `/add_premium <user_id> [months (default 1)]`", parse_mode="Markdown")
        return
    
    uid = int(c.args[0])
    months = int(c.args[1]) if len(c.args) > 1 else 1
    duration = months * 30 * 86400 # 30 days per month
    
    users = load_users()
    s_uid = str(uid)
    
    if uid in users["authorized"]:
        users["authorized"].remove(uid)
    
    # Calculate new expiry
    start_time = max(time.time(), users["premium"].get(s_uid, 0))
    users["premium"][s_uid] = start_time + duration
    
    save_users(users)
    expiry_date = time.strftime('%d-%m-%Y', time.localtime(users["premium"][s_uid]))
    await u.message.reply_text(f"💎 User `{uid}` upgraded to PREMIUM for *{months}* month(s).\n📅 *Expiry:* `{expiry_date}`", parse_mode="Markdown")

async def cmd_del_premium(u: Update, c: ContextTypes.DEFAULT_TYPE):
    if u.effective_user.id != OWNER_ID: return
    if not c.args: return
    uid = int(c.args[0])
    s_uid = str(uid)
    users = load_users()
    if s_uid in users["premium"]:
        users["premium"].pop(s_uid)
        if uid not in users["authorized"]:
            users["authorized"].append(uid)
        save_users(users)
    await u.message.reply_text(f"📉 User `{uid}` downgraded to Member.")

async def cmd_list(u: Update, c: ContextTypes.DEFAULT_TYPE):
    if u.effective_user.id != OWNER_ID: return
    u_data = load_users()
    text = "👥 *Bot Users*\n\n"
    text += f"👑 *Admin:* `{OWNER_ID}`\n\n"
    text += "💎 *Premium Subscribers:*\n"
    if u_data["premium"]:
        for uid, expiry in u_data["premium"].items():
            days_left = int((expiry - time.time()) / 86400)
            text += f"• `{uid}` — *{days_left}d left*\n"
    else:
        text += "None\n"
    
    text += "\n👤 *Authorized Members:*\n"
    text += ("\n".join([f"• `{uid}`" for uid in u_data["authorized"]]) if u_data["authorized"] else "None")
    await u.message.reply_text(text, parse_mode="Markdown")

async def cmd_downloads(u: Update, c: ContextTypes.DEFAULT_TYPE):
    files = [f for f in os.listdir(DOWNLOAD_DIR) if f.endswith((".mp4", ".mkv"))]
    if not files:
        await u.message.reply_text("📭 No downloads.")
        return
    txt = "📁 *CineHub Downloads*\n\n"
    for f in files:
        mb = os.path.getsize(os.path.join(DOWNLOAD_DIR, f)) / 1048576
        txt += f"• `{f}` — {mb:.0f} MB\n"
    await u.message.reply_text(txt, parse_mode="Markdown")

# --- LOGIN ---
async def login_start(u: Update, c: ContextTypes.DEFAULT_TYPE):
    if not auth(u.effective_user.id):
        await u.message.reply_text("❌ Unauthorized")
        return ConversationHandler.END
    await u.message.reply_text(
        "🔐 *CineHub Login*\n\nEnter your Stage.in *phone number:*",
        parse_mode="Markdown",
    )
    return LOGIN_PHONE

async def login_phone(u: Update, c: ContextTypes.DEFAULT_TYPE):
    phone = u.message.text.strip()
    c.user_data["phone"] = phone
    msg = await u.message.reply_text("⏳ *Connecting to Stage.in...*", parse_mode="Markdown")
    await c.bot.send_chat_action(chat_id=u.effective_chat.id, action="typing")

    loop = asyncio.get_event_loop()
    state, err = await loop.run_in_executor(None, run_login_phone, phone)

    if err:
        log.error(f"login_phone error: {err}")
        await msg.edit_text(f"❌ Connection failed: {err[:100]}\nTry /login again.")
        return ConversationHandler.END

    c.user_data["browser_state"] = state
    await msg.edit_text("📱 Enter the *OTP* sent to your phone:", parse_mode="Markdown")
    return LOGIN_OTP

# --- LOGIN HELPERS ---
async def run_login_phone(phone):
    """Async function to launch browser for phone submission."""
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        try:
            ctx = await browser.new_context(user_agent=UA, viewport={"width": 1280, "height": 720})
            pg = await ctx.new_page()
            await pg.route("**/*", lambda route: route.abort() if route.request.resource_type in {"image", "font"} else route.continue_())
            
            await pg.goto("https://www.stage.in/en/marathi", wait_until="commit", timeout=30000)
            await asyncio.sleep(2)
            try:
                await pg.locator('button:has-text("Login"), a:has-text("Login"), button:has-text("Sign")').first.click(timeout=5000)
                await asyncio.sleep(1)
            except: pass
            
            try:
                inp = pg.locator('input[type="tel"], input[placeholder*="phone"], input[placeholder*="mobile"]').first
                if await inp.is_visible():
                    await inp.fill(phone)
                    await asyncio.sleep(0.5)
                    await pg.locator('button[type="submit"], button:has-text("OTP"), button:has-text("Continue")').first.click()
                    await asyncio.sleep(1.5)
            except: pass
            
            state = await ctx.storage_state()
            return state, None
        except Exception as e:
            return None, str(e)
        finally:
            await browser.close()

async def run_login_otp(otp, prev_state):
    """Async function for OTP verification."""
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        try:
            ctx = await browser.new_context(storage_state=prev_state, user_agent=UA, viewport={"width": 1280, "height": 720})
            pg = await ctx.new_page()
            await pg.route("**/*", lambda route: route.abort() if route.request.resource_type in {"image", "font"} else route.continue_())
            
            await pg.goto("https://www.stage.in/en/marathi", wait_until="commit", timeout=30000)
            await asyncio.sleep(2)
            try:
                inputs = pg.locator('input[type="tel"], input[type="number"], input[maxlength="1"]')
                count = await inputs.count()
                if count >= 4:
                    for i, d in enumerate(otp[:count]):
                        await inputs.nth(i).fill(d)
                        await asyncio.sleep(0.1)
                else:
                    await pg.locator('input[type="tel"], input[placeholder*="OTP"]').first.fill(otp)
                await asyncio.sleep(0.5)
                await pg.locator('button[type="submit"], button:has-text("Verify"), button:has-text("Login")').first.click()
                await asyncio.sleep(4)
            except: pass
            
            await pg.goto("https://www.stage.in/en/marathi", wait_until="commit", timeout=20000)
            await asyncio.sleep(2)
            new_state = await ctx.storage_state()
            return new_state, None
        except Exception as e:
            return None, str(e)
        finally:
            await browser.close()

async def login_phone(u: Update, c: ContextTypes.DEFAULT_TYPE):
    phone = u.message.text.strip()
    c.user_data["phone"] = phone
    msg = await u.message.reply_text("⏳ *Connecting to Stage.in...*", parse_mode="Markdown")
    await c.bot.send_chat_action(chat_id=u.effective_chat.id, action="typing")

    state, err = await run_login_phone(phone)

    if err:
        log.error(f"login_phone error: {err}")
        await msg.edit_text(f"❌ Connection failed: {err[:100]}\nTry /login again.")
        return ConversationHandler.END

    c.user_data["browser_state"] = state
    await msg.edit_text("📱 Enter the *OTP* sent to your phone:", parse_mode="Markdown")
    return LOGIN_OTP

async def login_otp(u: Update, c: ContextTypes.DEFAULT_TYPE):
    otp = u.message.text.strip()
    prev = c.user_data.get("browser_state")
    msg = await u.message.reply_text("⏳ *Verifying OTP...*", parse_mode="Markdown")
    await c.bot.send_chat_action(chat_id=u.effective_chat.id, action="typing")

    state, err = await run_login_otp(otp, prev)

    if err:
        await msg.edit_text("❌ Verification failed. Please try again later or contact support.")
        return ConversationHandler.END

    save_session(state, "stage")
    await msg.edit_text("✅ *Logged in to Stage.in!* Session saved.\nSend a Stage.in URL to download.", parse_mode="Markdown")
    return ConversationHandler.END



async def login_cancel(u: Update, c: ContextTypes.DEFAULT_TYPE):
    await u.message.reply_text("❌ Cancelled.")
    return ConversationHandler.END



# --- URL HANDLER ---
async def handle_message(u: Update, c: ContextTypes.DEFAULT_TYPE):
    # Removed global auth check to allow everyone to see Movie Info
    text = u.message.text.strip()

    # Accept pasted session JSON or Netscape cookies
    processed = False
    if text.startswith("{"):
        try:
            data = json.loads(text)
            if isinstance(data, list): # Array of cookies
                cookies = data
                platform = detect_platform_from_cookies(cookies)
                if platform:
                    save_session(convert_to_storage_state(cookies), platform)
                    await u.message.reply_text(f"✅ Cookies imported for *{platform.capitalize()}*!", parse_mode="Markdown")
                    processed = True
            elif "cookies" in data: # Playwright storage_state
                platform = detect_platform_from_cookies(data["cookies"])
                if platform:
                    save_session(data, platform) # Already in correct format
                    await u.message.reply_text(f"✅ Session imported for *{platform.capitalize()}*!", parse_mode="Markdown")
                    processed = True
        except: pass
    elif "stage.in" in text:
        # Try parsing as Netscape format if keywords are present
        cookies = parse_netscape_cookies(text)
        if cookies:
            platform = detect_platform_from_cookies(cookies)
            if platform:
                save_session(convert_to_storage_state(cookies), platform)
                await u.message.reply_text(f"✅ Netscape cookies imported for *{platform.capitalize()}*!", parse_mode="Markdown")
                processed = True

    platform = "stage" if "stage.in" in text else None

    if not platform:
        return

    # Check session for the specific platform
    if not has_session("stage"):
        await u.message.reply_text("⚠️ Stage.in session missing. Use /login.")
        return

    url = text
    name = "video"
    batch_urls = []
    
    batch_match = re.search(r"/(\d+)-(\d+)$", url)
    cust_match = re.search(r"/(\d+)-(\d+)-(\d+)$", url)
    
    start_ep, end_ep, season, base_url = 0, 0, 1, ""
    if cust_match:
        season, start_ep, end_ep = int(cust_match.group(1)), int(cust_match.group(2)), int(cust_match.group(3))
        base_url = url[:cust_match.start()]
    elif batch_match:
        start_ep, end_ep = int(batch_match.group(1)), int(batch_match.group(2))
        base_url = url[:batch_match.start()]
        
    if base_url:
        if end_ep < start_ep or end_ep - start_ep > 30:
            await u.message.reply_text("❌ Invalid range or limit exceeded (30 max).")
            return
        for i in range(start_ep, end_ep + 1):
            ep_url = f"{base_url}/{i}"
            if "/watch/" not in ep_url:
                ep_url = re.sub(r"/(movie|show|episode)/(.+)$", r"//watch/", ep_url)
            batch_urls.append((ep_url, season, i))
        url = batch_urls[0][0]
    else:
        if "/watch/" not in url:
            url = re.sub(r"/(movie|show|episode)/(.+)$", r"//watch/", url)
        batch_urls = [(url, 1, 1)]
        
    m = re.search(r"/watch/([^?]+)", url)
    name = re.sub(r"[^\w\-]", "_", m.group(1)) if m else "stage_video"
    
    c.user_data["batch_urls"] = batch_urls

    batch_str = f"\n📦 *Batch Mode:* {len(batch_urls)} Episodes" if len(batch_urls) > 1 else ""
    msg = await u.message.reply_text(f"🎬 *{name}*{batch_str}\n\n🔍 Scraping qualities from *Stage.in*...", parse_mode="Markdown")
    try:
        m3u8, meta, err = await scrape_stage(url)
    except Exception as e:
        await msg.edit_text(f"❌ Scraper error: {str(e)[:200]}")
        return

    if not m3u8:
        await msg.edit_text(f"❌ {err}")
        return

    # Parse streams to show only available qualities
    try:
        async with httpx.AsyncClient(headers=HEADERS, timeout=30) as client:
            streams, _, _ = await parse_playlist_streams(m3u8, client)
    except Exception as e:
        await msg.edit_text(f"❌ Playlist error: {str(e)[:200]}")
        return

    if not streams:
        await msg.edit_text("❌ No streams found in playlist.")
        return

    # Store for callback
    c.user_data["m3u8"] = m3u8
    c.user_data["meta"] = meta
    c.user_data["streams"] = streams
    c.user_data["url"] = url
    c.user_data["name"] = name

    display = meta.get("title", name) if meta else name
    info = meta.get("info", "")
    desc = meta.get("description", "")
    poster_url = meta.get("poster", "")
    uid = u.effective_user.id
    is_auth = is_authorized(uid)

    msg_text = f"🎬 *{display}*\n"
    if info: msg_text += f"ℹ️ {info}\n"
    if desc: msg_text += f"\n📖 {desc}\n"
    
    if is_auth:
        msg_text += "\nChoose quality:"
        if not is_premium(uid):
            msg_text += "\n\n⚠️ *Some options marked [P] require Premium subscription.*"
        
        kb = []
        for i, s in enumerate(streams):
            res_text = res_to_label(s['res'])
            label = f"🎬 {res_text} ({s['res']})"
            res_val = int(res_text.replace('p', '')) if 'p' in res_text else 0
            if res_val > 720 and not is_premium(uid):
                label += " [P]"
            kb.append([InlineKeyboardButton(label, callback_data=f"q_{i}")])

        audio_label = "🎵 Audio Only"
        if not is_premium(uid):
            audio_label += " [P]"
        kb.append([InlineKeyboardButton(audio_label, callback_data="q_99")])
        kb.append([InlineKeyboardButton("❌ Cancel", callback_data="q_cancel")])
        markup = InlineKeyboardMarkup(kb)
    else:
        msg_text += "\n\n⚠️ *DOWNLOADS ARE RESTRICTED*\nYou must be an authorized member to download this file.\n\n💎 *Get Premium Access*\nUnlock all downloads, 4K quality, and audio extraction!"
        kb = [
            [InlineKeyboardButton("💎 View Subscription Plans", callback_data="show_plans")],
            [InlineKeyboardButton("✆ Admin RajGor_Paras", url="https://t.me/RajGor_Paras")]
        ]
        markup = InlineKeyboardMarkup(kb)

    if poster_url:
        try:
            await msg.delete()
            await u.message.reply_photo(
                photo=poster_url,
                caption=msg_text,
                reply_markup=markup,
                parse_mode="Markdown"
            )
            return
        except:
            pass

    await msg.edit_text(msg_text, reply_markup=markup, parse_mode="Markdown")

async def cb_quality(u: Update, c: ContextTypes.DEFAULT_TYPE):
    q = u.callback_query
    await q.answer()
    
    batch_urls = c.user_data.get("batch_urls")
    qi = q.data.replace("q_", "")
    is_audio = (qi == "audio")
    
    if not batch_urls:
        await q.message.edit_text("❌ Task expired. Send the link again.")
        return
        
    total = len(batch_urls)
    
    for idx, item in enumerate(batch_urls, 1):
        if isinstance(item, tuple) and len(item) == 3:
            b_url, season, ep_num = item
        else:
            b_url, season, ep_num = getattr(item, 'url', item), 1, idx
            
        m = re.search(r"/watch/([^?]+)", b_url)
        name = re.sub(r"[^\w\-]", "_", m.group(1)) if m else f"stage_video_ep{idx}"
        
        batch_prefix = f"📦 *[{idx}/{total}]* " if total > 1 else ""
        await q.message.edit_text(f"{batch_prefix}🎬 *{name}*\n🔍 Scraping playlist...", parse_mode="Markdown")
        
        try:
            m3u8, meta, err = await scrape_stage(b_url)
        except Exception as e:
            await q.message.reply_text(f"{batch_prefix}❌ Scraper error: {str(e)[:100]}")
            continue

        if not m3u8:
            await q.message.reply_text(f"{batch_prefix}❌ Failed: {err}")
            continue
            
        res = "Audio Only" if is_audio else f"{qi}p"
        
        display = meta.get("title", name)
        if total > 1:
            display = re.sub(r"\s*\|\s*Episode\s*\d+", "", display, flags=re.IGNORECASE).strip()
            display = f"{display} S{season:02d}E{ep_num:02d}"
        
        meta["title"] = display  # Enforce this title for the output filename inside download_hls

        await q.message.edit_text(f"{batch_prefix}🎬 *{display}*\n⏳ Starting download: *{res}*...", parse_mode="Markdown")

        last = [time.time(), ""]
        async def safe_edit(txt):
            try:
                await q.message.edit_text(txt, parse_mode="Markdown", disable_web_page_preview=True)
            except: pass

        async def cb(kind, txt):
            if time.time() - last[0] > 4 and txt != last[1]:
                last[0], last[1] = time.time(), txt
                try:
                    await safe_edit(f"{batch_prefix}🎬 *{display}*\n\n{txt}")
                except: pass

        try:
            if is_audio:
                result = await download_audio_only(m3u8, display, cb, meta)
            else:
                result = await download_hls(m3u8, display, qi, cb, meta)
        except Exception as e:
            await safe_edit(f"{batch_prefix}❌ Download error: {str(e)[:200]}")
            continue

        if not result:
            await safe_edit(f"{batch_prefix}❌ Download failed.")
            continue

        path, filename, res, size = result
        mb = size / 1048576
        ext = os.path.splitext(path)[1]
        poster_url = meta.get("poster", "")

        if rclone_is_ready():
            await safe_edit(f"{batch_prefix}☁️ *Uploading to Google Drive* ({mb:.0f} MB)...")
            async def gd_progress(pct, speed=""):
                await safe_edit(f"{batch_prefix}☁️ *Drive Upload*\n\n{progress_bar(pct)}\n💾 *{mb:.0f} MB*{speed}")
            
            link = await rclone_upload(path, f"{filename}{ext}", gd_progress)
            if link:
                cap_info = f"├ ℹ️ *Info:* {meta.get('info')}\n" if meta.get("info") else ""
                caption = (
                    f"━━━━━━━━━━━━━━━━━━━━━━━━\n"
                    f"   ✅ *UPLOAD COMPLETE*\n"
                    f"━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
                    f"🎬 *{display}*\n\n"
                    f"{cap_info}"
                    f"├ 📊 *Quality:* {res}\n"
                    f"├ 💾 *Size:* {mb:.0f} MB\n"
                    f"├ 📁 *File:* {filename}{ext}\n"
                    f"╰ ☁️ *Drive:* [Open Link]({link})\n\n"
                    f"━━━━━━━━━━━━━━━━━━━━━━━━\n"
                    f"_Powered by CineHub_ ⚡")
                if poster_url:
                    try:
                        await q.message.reply_photo(photo=poster_url, caption=caption, parse_mode="Markdown")
                        await safe_edit(f"✅ *{display}* — Uploaded!")
                    except: await safe_edit(caption)
                else: await safe_edit(caption)
                # Cleanup
                if os.path.exists(path): os.remove(path)
            else:
                await safe_edit(f"{batch_prefix}❌ Drive upload failed.")
        else:
            await safe_edit(f"{batch_prefix}✅ Downloaded! Check {os.path.basename(path)}")
            
    if total > 1:
        await c.bot.send_message(chat_id=u.effective_chat.id, text=f"🎉 *Batch Task Complete!*\nSuccessfully processed {total} episodes.", parse_mode="Markdown")

# ---

# --- COOKIE FILE HANDLER ---
async def handle_document(u: Update, c: ContextTypes.DEFAULT_TYPE):
    """Handle uploaded .txt or .json cookie files."""
    if not auth(u.effective_user.id): return
    
    doc = u.message.document
    if not (doc.file_name.lower().endswith(".txt") or doc.file_name.lower().endswith(".json")):
        return

    msg = await u.message.reply_text("📥 *Processing cookie file...*", parse_mode="Markdown")
    try:
        file = await doc.get_file()
        # Create a temp buffer to read content
        buffer = await file.download_as_bytearray()
        content = buffer.decode("utf-8", errors="ignore").strip()

        platform = None
        cookies = []
        
        if content.startswith("{"):
            # Try JSON parsing
            try:
                data = json.loads(content)
                if isinstance(data, list): # Array of cookies
                    cookies = data
                elif "cookies" in data: # storage_state
                    cookies = data["cookies"]
                
                platform = detect_platform_from_cookies(cookies)
                if platform:
                    # If it was storage_state, use it directly (might have origins)
                    state = data if "cookies" in data and not isinstance(data, list) else convert_to_storage_state(cookies)
                    save_session(state, platform)
            except Exception as e:
                log.error(f"JSON cookie parse error: {e}")
        
        if not platform:
            # Try Netscape parsing
            cookies = parse_netscape_cookies(content)
            platform = detect_platform_from_cookies(cookies)
            if platform:
                save_session(convert_to_storage_state(cookies), platform)

        if platform:
            await msg.edit_text(f"✅ *Cookie Import Successful!*\n\nPlatform: `{platform.capitalize()}`\nCookies: `{len(cookies)}` imported.", parse_mode="Markdown")
        else:
            await msg.edit_text("❌ *Import Failed*\nNo valid cookies for Stage.in found in this file.", parse_mode="Markdown")
            
    except Exception as e:
        log.error(f"Handle document error: {e}")
        await msg.edit_text(f"❌ *Error processing file:*\n`{str(e)[:100]}`", parse_mode="Markdown")



# --- /gdrive — rclone status check ---
async def cmd_gdrive(u: Update, c: ContextTypes.DEFAULT_TYPE):
    if not auth(u.effective_user.id):
        await u.message.reply_text("❌ Unauthorized")
        return

    if rclone_is_ready():
        await u.message.reply_text(
            f"✅ *Google Drive is ready!*\n\n"
            f"Remote: `{RCLONE_REMOTE}`\n"
            f"Folder: `{RCLONE_FOLDER}`\n\n"
            f"Files will auto-upload after download.",
            parse_mode="Markdown")
    else:
        await u.message.reply_text(
            "⚠️ *Google Drive Setup (rclone)*\n\n"
            "rclone is not configured yet. Run this on your PC:\n\n"
            "```\nrclone config\n```\n\n"
            "Steps:\n"
            "1️⃣ Choose `n` (new remote)\n"
            "2️⃣ Name it `drive`\n"
            "3️⃣ Choose `drive` (Google Drive)\n"
            "4️⃣ Leave client ID/secret blank\n"
            "5️⃣ Choose scope `1` (full access)\n"
            "6️⃣ Follow the auth link\n"
            "7️⃣ Done! Restart the bot\n\n"
            "💡 This is a one-time setup.",
            parse_mode="Markdown")


# ============================================================
#  MAIN
# ============================================================

async def post_init(application: Application) -> None:
    commands = [
        BotCommand("start", "Start the bot & show info"),
        BotCommand("help", "Show download instructions"),
        BotCommand("premium", "View premium subscription plans"),
        BotCommand("me", "View your account status"),
        BotCommand("login", "Login to Stage.in using your phone number"),
        BotCommand("downloads", "List local downloads (Admin)"),
        BotCommand("gdrive", "Upload local files to Drive (Admin)"),
    ]
    await application.bot.set_my_commands(commands)

def main():
    if BOT_TOKEN == "YOUR_BOT_TOKEN_HERE":
        print("\n  CineHub — Set your bot token:\n")
        print("  Linux:   export CINEHUB_TOKEN=your_token")
        print("  Windows: set CINEHUB_TOKEN=your_token")
        print("  Or edit BOT_TOKEN in cinehub.py\n")
        return

    print("╔═══════════════════════════════════════╗")
    print("║       🎬 CineHub Bot v3.1.0           ║")
    print("║       ☁️ rclone Drive • 🎵 Audio       ║")
    print("╚═══════════════════════════════════════╝")
    print(f"  Downloads : {DOWNLOAD_DIR}")
    print(f"  Stage.in  : {'✅ Active' if has_session('stage') else '❌ /login needed'}")
    print(f"  rclone    : {'✅ ' + RCLONE_REMOTE + ': ready' if rclone_is_ready() else '⚠️ run: rclone config'}")
    print(f"  Starting...\n")

    app = (Application.builder()
           .token(BOT_TOKEN)
           .read_timeout(30)
           .write_timeout(30)
           .post_init(post_init)
           .build())

    app.add_handler(ConversationHandler(
        entry_points=[CommandHandler("login", login_start)],
        states={
            LOGIN_PHONE: [MessageHandler(filters.TEXT & ~filters.COMMAND, login_phone)],
            LOGIN_OTP: [MessageHandler(filters.TEXT & ~filters.COMMAND, login_otp)],
        },
        fallbacks=[CommandHandler("cancel", login_cancel)],
    ))
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("premium", cmd_premium))
    app.add_handler(CommandHandler("me", cmd_me))
    app.add_handler(CommandHandler("downloads", cmd_downloads))
    app.add_handler(CommandHandler("gdrive", cmd_gdrive))
    
    # Admin commands
    app.add_handler(CommandHandler("auth", cmd_auth))
    app.add_handler(CommandHandler("unauth", cmd_unauth))
    app.add_handler(CommandHandler("add_premium", cmd_add_premium))
    app.add_handler(CommandHandler("del_premium", cmd_del_premium))
    app.add_handler(CommandHandler("list", cmd_list))

    app.add_handler(CallbackQueryHandler(cb_quality, pattern="^q_"))
    app.add_handler(MessageHandler(filters.Document.ALL, handle_document))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    print(f"  🤖 Bot running! OWNER: {OWNER_ID}")
    app.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()
