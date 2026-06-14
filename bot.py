"""
TeraBox Telegram Bot — 3 Working RapidAPI Endpoints
"""

import asyncio
import logging
import os
import tempfile
import traceback
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx
from dotenv import load_dotenv
from fastapi import FastAPI, Request, Response
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    MessageHandler,
    filters,
    ContextTypes,
)

load_dotenv()

logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    level=logging.INFO,
)
logging.getLogger("httpx").setLevel(logging.WARNING)
logger = logging.getLogger(__name__)

BOT_TOKEN    = os.getenv("BOT_TOKEN", "")
WEBHOOK_URL  = os.getenv("WEBHOOK_URL", "")
PORT         = int(os.getenv("PORT", "10000"))
COOKIE       = os.getenv("COOKIE", "")
GPLINK_API   = os.getenv("GPLINK_API", "")
FREE_LINKS   = int(os.getenv("FREE_LINKS", "3"))
COOLDOWN_HRS = int(os.getenv("COOLDOWN_HRS", "7"))
MONGO_URI    = os.getenv("MONGO_URI", "")
RAPIDAPI_KEY = os.getenv("RAPIDAPI_KEY", "")

TELEGRAM_MAX_BYTES = 50 * 1024 * 1024
WARN_SIZE_BYTES    = 45 * 1024 * 1024

if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN set nahi hai!")

# ── MongoDB ──────────────────────────────────────────────────────────────────

_col = None

def get_col():
    global _col
    if _col is not None:
        return _col
    if MONGO_URI:
        from pymongo import MongoClient
        client = MongoClient(MONGO_URI, serverSelectionTimeoutMS=5000)
        _col = client["terabox_bot"]["users"]
        logger.info("MongoDB connected")
    else:
        _col = _Mem()
        logger.warning("MONGO_URI nahi — in-memory use ho rahi hai")
    return _col


class _Mem:
    def __init__(self):
        self._d = {}
    def find_one(self, q):
        return self._d.get(q["user_id"])
    def update_one(self, q, upd, upsert=False):
        uid = q["user_id"]
        if uid not in self._d:
            if upsert:
                self._d[uid] = {"user_id": uid, "link_count": 0,
                                "ad_shown_at": None, "reset_at": None}
            else:
                return
        self._d[uid].update(upd.get("$set", {}))


def init_db():
    col = get_col()
    if MONGO_URI:
        col.find_one({"user_id": 0})
        logger.info("MongoDB ready")


def get_user(user_id):
    doc = get_col().find_one({"user_id": user_id})
    if doc is None:
        return {"user_id": user_id, "link_count": 0,
                "ad_shown_at": None, "reset_at": None}
    doc.pop("_id", None)
    return doc


def save_user(data):
    d = {k: v for k, v in data.items() if k != "_id"}
    get_col().update_one({"user_id": data["user_id"]}, {"$set": d}, upsert=True)

# ── Gate logic ───────────────────────────────────────────────────────────────

def _now():
    return datetime.now(timezone.utc)

def _ts(ts):
    if ts is None:
        return None
    if isinstance(ts, datetime):
        return ts.replace(tzinfo=timezone.utc) if ts.tzinfo is None else ts
    return datetime.fromisoformat(str(ts))

class Gate:
    ALLOW    = "allow"
    SHOW_AD  = "show_ad"
    COOLDOWN = "cooldown"

def check_gate(user):
    now = _now()
    reset_at = _ts(user.get("reset_at"))
    if reset_at and now < reset_at:
        return Gate.COOLDOWN, reset_at - now
    if reset_at and now >= reset_at:
        user["link_count"]  = 0
        user["reset_at"]    = None
        user["ad_shown_at"] = None
    if user["link_count"] < FREE_LINKS:
        return Gate.ALLOW, None
    return Gate.SHOW_AD, None

def increment_count(user):
    user["link_count"] += 1
    save_user(user)

def set_ad_shown(user):
    now = _now()
    user["ad_shown_at"] = now.isoformat()
    user["reset_at"]    = (now + timedelta(hours=COOLDOWN_HRS)).isoformat()
    save_user(user)

def fmt_td(td):
    total = int(td.total_seconds())
    hrs   = total // 3600
    mins  = (total % 3600) // 60
    if hrs > 0 and mins > 0:
        return f"{hrs} ghante {mins} minute"
    if hrs > 0:
        return f"{hrs} ghante"
    return f"{mins} minute"

# ── GPLink ───────────────────────────────────────────────────────────────────

async def gplink_shorten(url):
    if not GPLINK_API:
        return url
    try:
        async with httpx.AsyncClient(timeout=10) as c:
            r = await c.get(
                f"https://api.gplinks.com/api?api={GPLINK_API}&url={url}&format=text"
            )
            r.raise_for_status()
            s = r.text.strip()
            if s.startswith("http"):
                return s
    except Exception as e:
        logger.error("GPLink error: %s", e)
    return url

# ── Terabox Domains ───────────────────────────────────────────────────────────

TERABOX_DOMAINS = (
    "terabox.com", "1024terabox.com", "terabox.app",
    "teraboxapp.com", "terasharefile.com", "1024tera.com",
    "terafileshare.com", "4funbox.com", "mirrobox.com",
    "nephobox.com", "freeterabox.com", "momerybox.com",
    "tibibox.com", "sendcm.com", "gibibox.com",
    "terabox.fun", "terabox.cc", "terabox.link",
)

def is_terabox(text):
    return any(d in text.lower() for d in TERABOX_DOMAINS)

def human_size(n):
    for u in ("B", "KB", "MB", "GB"):
        if abs(n) < 1024:
            return f"{n:.1f} {u}"
        n /= 1024
    return f"{n:.1f} TB"

# ── Terabox APIs — 3 working endpoints ───────────────────────────────────────

async def fetch_info(url):
    errors = []

    # ── API 1: Terabox Downloader + Online Viewer (WORKING ✅) ──
    if RAPIDAPI_KEY:
        try:
            async with httpx.AsyncClient(timeout=30) as c:
                r = await c.get(
                    "https://terabox-downloader-online-viewer-player-api.p.rapidapi.com/data",
                    params={"url": url},
                    headers={
                        "x-rapidapi-key":  RAPIDAPI_KEY,
                        "x-rapidapi-host": "terabox-downloader-online-viewer-player-api.p.rapidapi.com",
                    },
                )
                r.raise_for_status()
                data = r.json()
                logger.info("API1 raw: %s", str(data)[:400])

            # Is API ka response format:
            # { status: "success", data: { structure: { file_name, download_url, direct_link, ... } } }
            if isinstance(data, dict) and data.get("status") == "success":
                s = data.get("data", {}).get("structure", {})
                dl = s.get("direct_link") or s.get("download_url") or s.get("dlink") or ""
                if dl and dl.startswith("http"):
                    logger.info("API 1 (viewer) success ✅")
                    return {
                        "file_name":     s.get("file_name") or "video.mp4",
                        "file_size":     _parse_size(s.get("size", "0")),
                        "download_link": dl,
                        "thumbnail":     s.get("thumb") or s.get("thumbnail"),
                    }
        except Exception as e:
            errors.append(f"API1: {e}")
            logger.warning("API 1 fail: %s", e)

    # ── API 2: downloader endpoint (same API) ──
    if RAPIDAPI_KEY:
        try:
            async with httpx.AsyncClient(timeout=30) as c:
                r = await c.get(
                    "https://terabox-downloader-online-viewer-player-api.p.rapidapi.com/downloader",
                    params={"url": url},
                    headers={
                        "x-rapidapi-key":  RAPIDAPI_KEY,
                        "x-rapidapi-host": "terabox-downloader-online-viewer-player-api.p.rapidapi.com",
                    },
                )
                r.raise_for_status()
                data = r.json()
                logger.info("API2 raw: %s", str(data)[:400])

            result = _parse_rapid(data)
            if result:
                logger.info("API 2 success ✅")
                return result
        except Exception as e:
            errors.append(f"API2: {e}")
            logger.warning("API 2 fail: %s", e)

    # ── API 3: Worker fallback ──
    try:
        async with httpx.AsyncClient(timeout=25) as c:
            r = await c.post(
                "https://terabox.hnn.workers.dev/api/get-info",
                json={"url": url},
                headers={"User-Agent": "Mozilla/5.0"},
            )
            r.raise_for_status()
            data = r.json()
        if data.get("ok"):
            files = data.get("list", [])
            if files:
                f  = files[0]
                dl = f.get("dlink") or f.get("download_url") or ""
                if dl:
                    logger.info("Worker API success ✅")
                    return {
                        "file_name":     f.get("filename") or "file",
                        "file_size":     int(f.get("size", 0)),
                        "download_link": dl,
                        "thumbnail":     f.get("thumb"),
                    }
    except Exception as e:
        errors.append(f"Worker: {e}")
        logger.warning("Worker fail: %s", e)

    # ── API 4: Worker fallback (no key needed) ──
    try:
        async with httpx.AsyncClient(timeout=25) as c:
            r = await c.post(
                "https://terabox.hnn.workers.dev/api/get-info",
                json={"url": url},
                headers={"User-Agent": "Mozilla/5.0"},
            )
            r.raise_for_status()
            data = r.json()

        if data.get("ok"):
            files = data.get("list", [])
            if files:
                f  = files[0]
                dl = f.get("dlink") or f.get("download_url") or f.get("url", "")
                if dl:
                    logger.info("Worker API success ✅")
                    return {
                        "file_name":     f.get("filename") or "file",
                        "file_size":     int(f.get("size", 0)),
                        "download_link": dl,
                        "thumbnail":     f.get("thumb"),
                    }
        errors.append(f"Worker: {data.get('message')}")
    except Exception as e:
        errors.append(f"Worker: {e}")
        logger.warning("Worker fail: %s", e)

    raise ValueError(
        "❌ Terabox se file nahi mili.\n\n"
        "Possible reasons:\n"
        "• Link private/expire ho gaya\n"
        "• Terabox server down hai\n\n"
        "Thodi der baad dobara try karein."
    )


def _parse_rapid(data):
    """RapidAPI response se download link nikalo — har format handle karo."""
    if not data:
        return None

    # List format
    if isinstance(data, list):
        data = data[0] if data else {}

    # Nested 'response' key
    if isinstance(data, dict) and "response" in data:
        inner = data["response"]
        if isinstance(inner, list) and inner:
            data = inner[0]
        elif isinstance(inner, dict):
            data = inner

    if not isinstance(data, dict):
        return None

    # Download link dhundo
    dl = (
        data.get("url") or
        data.get("download_url") or
        data.get("dlink") or
        data.get("download_link") or
        data.get("fast_download_link") or
        data.get("hd_mp4_url") or
        data.get("mp4_url") or
        ""
    )

    # resolutions ke andar dhundo
    if not dl:
        res = data.get("resolutions") or data.get("resolution") or {}
        if isinstance(res, dict):
            dl = (
                res.get("Fast Download") or
                res.get("HD Video") or
                res.get("SD Video") or
                res.get("Auto") or
                next(iter(res.values()), "")
            )

    if not dl or not str(dl).startswith("http"):
        return None

    return {
        "file_name":     (data.get("title") or data.get("file_name") or
                         data.get("name") or data.get("filename") or "video.mp4"),
        "file_size":     int(data.get("size", 0)),
        "download_link": dl,
        "thumbnail":     data.get("thumbnail") or data.get("thumb") or data.get("cover"),
    }

# ── Bot Handlers ─────────────────────────────────────────────────────────────

HELP_TEXT = (
    "🤖 *TeraBox Downloader Bot*\n\n"
    "Terabox link paste karo — file/video mil jaayegi!\n\n"
    f"🎁 Pehle *{FREE_LINKS}* links free hain\n"
    "Uske baad ek chhoti ad dekhni hogi\n\n"
    "/status — apna quota dekho"
)


async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    name = update.effective_user.first_name or "Dost"
    user = get_user(update.effective_user.id)
    gate, _ = check_gate(user)
    left = max(0, FREE_LINKS - user["link_count"]) if gate == Gate.ALLOW else FREE_LINKS
    await update.message.reply_text(
        f"Aao *{name}*! 👋\n\n{HELP_TEXT}\n\n"
        f"📊 Abhi *{left}* free link(s) baaki hain.",
        parse_mode=ParseMode.MARKDOWN,
    )


async def cmd_help(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(HELP_TEXT, parse_mode=ParseMode.MARKDOWN)


async def cmd_status(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    user = get_user(update.effective_user.id)
    gate, remaining = check_gate(user)
    if gate == Gate.COOLDOWN:
        txt = f"⏳ *Cooldown:* `{fmt_td(remaining)}` baad {FREE_LINKS} links milenge."
    elif gate == Gate.SHOW_AD:
        txt = "📢 Agli link bhejein — ad link milega, kholo, file milegi!"
    else:
        left = FREE_LINKS - user["link_count"]
        txt  = f"✅ *{left}* free link(s) baaki hain!"
    await update.message.reply_text(txt, parse_mode=ParseMode.MARKDOWN)


async def handle_link(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    msg     = update.message
    text    = (msg.text or "").strip()
    user_id = update.effective_user.id

    if not is_terabox(text):
        await msg.reply_text(
            "❓ Yeh Terabox link nahi lag raha.\n"
            "Valid URL paste karein (e.g. https://1024terabox.com/s/...)"
        )
        return

    user = get_user(user_id)
    gate, remaining = check_gate(user)

    if gate == Gate.COOLDOWN:
        await msg.reply_text(
            f"⏳ *Ruko!* `{fmt_td(remaining)}` baad access milega.",
            parse_mode=ParseMode.MARKDOWN,
        )
        return

    if gate == Gate.SHOW_AD:
        wait = await msg.reply_text("🔗 Ad link bana raha hoon...")
        ad   = await gplink_shorten(text)
        set_ad_shown(user)
        body = (
            f"📢 *{FREE_LINKS} free links use ho gaye!*\n\n"
            "👇 Button dabao → ad dekho → file pao\n\n"
            f"✅ Ad ke baad *{COOLDOWN_HRS} ghante* mein phir *{FREE_LINKS} free links*!"
        ) if GPLINK_API else "Seedha link:"
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("🎬 Ad Dekho & File Pao", url=ad)],
            [InlineKeyboardButton("📊 Mera Status", callback_data="status")],
        ])
        await wait.edit_text(body, parse_mode=ParseMode.MARKDOWN, reply_markup=kb)
        return

    sm = await msg.reply_text("⏳ Link check ho rahi hai...")

    try:
        info = await fetch_info(text)
    except httpx.TimeoutException:
        await sm.edit_text(
            "⏰ *Timeout!* Terabox slow hai. Dobara try karein.",
            parse_mode=ParseMode.MARKDOWN,
        )
        return
    except ValueError as e:
        await sm.edit_text(str(e), parse_mode=ParseMode.MARKDOWN)
        return
    except Exception:
        logger.error(traceback.format_exc())
        await sm.edit_text("💥 Unexpected error. Dobara try karein.")
        return

    increment_count(user)
    fresh = get_user(user_id)
    left  = max(0, FREE_LINKS - fresh["link_count"])

    fname   = info["file_name"]
    fsize   = info["file_size"]
    dl      = info["download_link"]
    caption = f"📁 *{fname}*\n📦 Size: `{human_size(fsize) if fsize else '?'}`"
    caption += (
        f"\n\n✅ *{left}* free link(s) aur baaki"
        if left > 0
        else "\n\n⚠️ Agli baar ad dekhna hoga"
    )

    if fsize and fsize > TELEGRAM_MAX_BYTES:
        kb = InlineKeyboardMarkup([[InlineKeyboardButton("⬇️ Direct Download", url=dl)]])
        await sm.edit_text(
            f"{caption}\n\n⚠️ *50 MB se badi file* — seedha download karo:",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=kb,
        )
        return

    note = "Badi file, thoda wait..." if fsize and fsize > WARN_SIZE_BYTES else "File aa rahi hai..."
    await sm.edit_text(f"{caption}\n\n📤 {note}", parse_mode=ParseMode.MARKDOWN)

    try:
        await _send_file(update, ctx, info, caption)
        await sm.delete()
    except _Big:
        kb = InlineKeyboardMarkup([[InlineKeyboardButton("⬇️ Direct Download", url=dl)]])
        await sm.edit_text(
            f"{caption}\n\n⚠️ Telegram ne reject kiya.",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=kb,
        )
    except Exception:
        logger.error(traceback.format_exc())
        kb = InlineKeyboardMarkup([[InlineKeyboardButton("⬇️ Direct Download", url=dl)]])
        await sm.edit_text(
            f"{caption}\n\n😓 Send nahi hua, direct link lo:",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=kb,
        )


async def handle_cb(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    if q.data == "status":
        await cmd_status(update, ctx)


class _Big(Exception):
    pass


async def _send_file(update, ctx, info, caption):
    dl    = info["download_link"]
    fname = info["file_name"]
    cid   = update.effective_chat.id
    if not dl:
        raise ValueError("Download link empty")

    ext      = Path(fname).suffix.lower()
    is_video = ext in (".mp4", ".mkv", ".avi", ".mov", ".webm", ".flv", ".m4v")

    with tempfile.NamedTemporaryFile(delete=False, suffix=ext or ".bin") as tmp:
        path = tmp.name

    try:
        async with httpx.AsyncClient(
            timeout=300,
            follow_redirects=True,
            headers={"User-Agent": "Mozilla/5.0"},
        ) as c:
            async with c.stream("GET", dl) as r:
                r.raise_for_status()
                done = 0
                with open(path, "wb") as f:
                    async for chunk in r.aiter_bytes(256 * 1024):
                        done += len(chunk)
                        if done > TELEGRAM_MAX_BYTES:
                            raise _Big()
                        f.write(chunk)

        with open(path, "rb") as f:
            kw = dict(
                chat_id=cid,
                filename=fname,
                caption=caption,
                parse_mode=ParseMode.MARKDOWN,
                read_timeout=120,
                write_timeout=120,
            )
            if is_video:
                await ctx.bot.send_video(video=f, supports_streaming=True, **kw)
            else:
                await ctx.bot.send_document(document=f, **kw)
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass

# ── Self ping ────────────────────────────────────────────────────────────────

async def ping_loop(url, interval=840):
    await asyncio.sleep(30)
    async with httpx.AsyncClient(timeout=10) as c:
        while True:
            try:
                r = await c.get(f"{url}/health")
                logger.info("Ping: %s", r.status_code)
            except Exception as e:
                logger.warning("Ping fail: %s", e)
            await asyncio.sleep(interval)

# ── App ──────────────────────────────────────────────────────────────────────

def build_ptb():
    app = Application.builder().token(BOT_TOKEN).read_timeout(60).write_timeout(120).build()
    app.add_handler(CommandHandler("start",  cmd_start))
    app.add_handler(CommandHandler("help",   cmd_help))
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CallbackQueryHandler(handle_cb))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_link))
    return app


def create_app():
    init_db()
    ptb  = build_ptb()
    fast = FastAPI(title="TeraBox Bot")

    @fast.on_event("startup")
    async def startup():
        await ptb.initialize()
        if WEBHOOK_URL:
            wh = f"{WEBHOOK_URL.rstrip('/')}/webhook"
            await ptb.bot.set_webhook(
                url=wh,
                allowed_updates=Update.ALL_TYPES,
                drop_pending_updates=True,
            )
            logger.info("Webhook: %s", wh)
            asyncio.create_task(ping_loop(WEBHOOK_URL.rstrip("/")))
        else:
            logger.warning("WEBHOOK_URL nahi — polling mode use karo")
        await ptb.start()

    @fast.on_event("shutdown")
    async def shutdown():
        await ptb.stop()
        await ptb.shutdown()

    @fast.post("/webhook")
    async def webhook(req: Request):
        data = await req.json()
        await ptb.process_update(Update.de_json(data=data, bot=ptb.bot))
        return Response(status_code=200)

    @fast.get("/health")
    async def health():
        me = await ptb.bot.get_me()
        return {
            "status":     "ok",
            "bot":        me.username,
            "free_links": FREE_LINKS,
            "rapidapi":   "set" if RAPIDAPI_KEY else "MISSING",
            "db":         "mongodb" if MONGO_URI else "memory",
        }

    return fast


def run_polling():
    init_db()
    build_ptb().run_polling(allowed_updates=Update.ALL_TYPES, drop_pending_updates=True)


def run_webhook():
    import uvicorn
    uvicorn.run(create_app(), host="0.0.0.0", port=PORT, log_level="info")


if __name__ == "__main__":
    import sys
    if "--polling" in sys.argv:
        run_polling()
    else:
        run_webhook()
"""
TeraBox Telegram Bot — 3 Working RapidAPI Endpoints
"""

import asyncio
import logging
import os
import tempfile
import traceback
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx
from dotenv import load_dotenv
from fastapi import FastAPI, Request, Response
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    MessageHandler,
    filters,
    ContextTypes,
)

load_dotenv()

logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    level=logging.INFO,
)
logging.getLogger("httpx").setLevel(logging.WARNING)
logger = logging.getLogger(__name__)

BOT_TOKEN    = os.getenv("BOT_TOKEN", "")
WEBHOOK_URL  = os.getenv("WEBHOOK_URL", "")
PORT         = int(os.getenv("PORT", "10000"))
COOKIE       = os.getenv("COOKIE", "")
GPLINK_API   = os.getenv("GPLINK_API", "")
FREE_LINKS   = int(os.getenv("FREE_LINKS", "3"))
COOLDOWN_HRS = int(os.getenv("COOLDOWN_HRS", "7"))
MONGO_URI    = os.getenv("MONGO_URI", "")
RAPIDAPI_KEY = os.getenv("RAPIDAPI_KEY", "")

TELEGRAM_MAX_BYTES = 50 * 1024 * 1024
WARN_SIZE_BYTES    = 45 * 1024 * 1024

if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN set nahi hai!")

# ── MongoDB ──────────────────────────────────────────────────────────────────

_col = None

def get_col():
    global _col
    if _col is not None:
        return _col
    if MONGO_URI:
        from pymongo import MongoClient
        client = MongoClient(MONGO_URI, serverSelectionTimeoutMS=5000)
        _col = client["terabox_bot"]["users"]
        logger.info("MongoDB connected")
    else:
        _col = _Mem()
        logger.warning("MONGO_URI nahi — in-memory use ho rahi hai")
    return _col


class _Mem:
    def __init__(self):
        self._d = {}
    def find_one(self, q):
        return self._d.get(q["user_id"])
    def update_one(self, q, upd, upsert=False):
        uid = q["user_id"]
        if uid not in self._d:
            if upsert:
                self._d[uid] = {"user_id": uid, "link_count": 0,
                                "ad_shown_at": None, "reset_at": None}
            else:
                return
        self._d[uid].update(upd.get("$set", {}))


def init_db():
    col = get_col()
    if MONGO_URI:
        col.find_one({"user_id": 0})
        logger.info("MongoDB ready")


def get_user(user_id):
    doc = get_col().find_one({"user_id": user_id})
    if doc is None:
        return {"user_id": user_id, "link_count": 0,
                "ad_shown_at": None, "reset_at": None}
    doc.pop("_id", None)
    return doc


def save_user(data):
    d = {k: v for k, v in data.items() if k != "_id"}
    get_col().update_one({"user_id": data["user_id"]}, {"$set": d}, upsert=True)

# ── Gate logic ───────────────────────────────────────────────────────────────

def _now():
    return datetime.now(timezone.utc)

def _ts(ts):
    if ts is None:
        return None
    if isinstance(ts, datetime):
        return ts.replace(tzinfo=timezone.utc) if ts.tzinfo is None else ts
    return datetime.fromisoformat(str(ts))

class Gate:
    ALLOW    = "allow"
    SHOW_AD  = "show_ad"
    COOLDOWN = "cooldown"

def check_gate(user):
    now = _now()
    reset_at = _ts(user.get("reset_at"))
    if reset_at and now < reset_at:
        return Gate.COOLDOWN, reset_at - now
    if reset_at and now >= reset_at:
        user["link_count"]  = 0
        user["reset_at"]    = None
        user["ad_shown_at"] = None
    if user["link_count"] < FREE_LINKS:
        return Gate.ALLOW, None
    return Gate.SHOW_AD, None

def increment_count(user):
    user["link_count"] += 1
    save_user(user)

def set_ad_shown(user):
    now = _now()
    user["ad_shown_at"] = now.isoformat()
    user["reset_at"]    = (now + timedelta(hours=COOLDOWN_HRS)).isoformat()
    save_user(user)

def fmt_td(td):
    total = int(td.total_seconds())
    hrs   = total // 3600
    mins  = (total % 3600) // 60
    if hrs > 0 and mins > 0:
        return f"{hrs} ghante {mins} minute"
    if hrs > 0:
        return f"{hrs} ghante"
    return f"{mins} minute"

# ── GPLink ───────────────────────────────────────────────────────────────────

async def gplink_shorten(url):
    if not GPLINK_API:
        return url
    try:
        async with httpx.AsyncClient(timeout=10) as c:
            r = await c.get(
                f"https://api.gplinks.com/api?api={GPLINK_API}&url={url}&format=text"
            )
            r.raise_for_status()
            s = r.text.strip()
            if s.startswith("http"):
                return s
    except Exception as e:
        logger.error("GPLink error: %s", e)
    return url

# ── Terabox Domains ───────────────────────────────────────────────────────────

TERABOX_DOMAINS = (
    "terabox.com", "1024terabox.com", "terabox.app",
    "teraboxapp.com", "terasharefile.com", "1024tera.com",
    "terafileshare.com", "4funbox.com", "mirrobox.com",
    "nephobox.com", "freeterabox.com", "momerybox.com",
    "tibibox.com", "sendcm.com", "gibibox.com",
    "terabox.fun", "terabox.cc", "terabox.link",
)

def is_terabox(text):
    return any(d in text.lower() for d in TERABOX_DOMAINS)

def human_size(n):
    for u in ("B", "KB", "MB", "GB"):
        if abs(n) < 1024:
            return f"{n:.1f} {u}"
        n /= 1024
    return f"{n:.1f} TB"

# ── Terabox APIs — 3 working endpoints ───────────────────────────────────────

async def fetch_info(url):
    errors = []

    # ── API 1: Terabox Downloader + Online Viewer (WORKING ✅) ──
    if RAPIDAPI_KEY:
        try:
            async with httpx.AsyncClient(timeout=30) as c:
                r = await c.get(
                    "https://terabox-downloader-online-viewer-player-api.p.rapidapi.com/data",
                    params={"url": url},
                    headers={
                        "x-rapidapi-key":  RAPIDAPI_KEY,
                        "x-rapidapi-host": "terabox-downloader-online-viewer-player-api.p.rapidapi.com",
                    },
                )
                r.raise_for_status()
                data = r.json()
                logger.info("API1 raw: %s", str(data)[:400])

            # Is API ka response format:
            # { status: "success", data: { structure: { file_name, download_url, direct_link, ... } } }
            if isinstance(data, dict) and data.get("status") == "success":
                s = data.get("data", {}).get("structure", {})
                dl = s.get("direct_link") or s.get("download_url") or s.get("dlink") or ""
                if dl and dl.startswith("http"):
                    logger.info("API 1 (viewer) success ✅")
                    return {
                        "file_name":     s.get("file_name") or "video.mp4",
                        "file_size":     _parse_size(s.get("size", "0")),
                        "download_link": dl,
                        "thumbnail":     s.get("thumb") or s.get("thumbnail"),
                    }
        except Exception as e:
            errors.append(f"API1: {e}")
            logger.warning("API 1 fail: %s", e)

    # ── API 2: downloader endpoint (same API) ──
    if RAPIDAPI_KEY:
        try:
            async with httpx.AsyncClient(timeout=30) as c:
                r = await c.get(
                    "https://terabox-downloader-online-viewer-player-api.p.rapidapi.com/downloader",
                    params={"url": url},
                    headers={
                        "x-rapidapi-key":  RAPIDAPI_KEY,
                        "x-rapidapi-host": "terabox-downloader-online-viewer-player-api.p.rapidapi.com",
                    },
                )
                r.raise_for_status()
                data = r.json()
                logger.info("API2 raw: %s", str(data)[:400])

            result = _parse_rapid(data)
            if result:
                logger.info("API 2 success ✅")
                return result
        except Exception as e:
            errors.append(f"API2: {e}")
            logger.warning("API 2 fail: %s", e)

    # ── API 3: Worker fallback ──
    try:
        async with httpx.AsyncClient(timeout=25) as c:
            r = await c.post(
                "https://terabox.hnn.workers.dev/api/get-info",
                json={"url": url},
                headers={"User-Agent": "Mozilla/5.0"},
            )
            r.raise_for_status()
            data = r.json()
        if data.get("ok"):
            files = data.get("list", [])
            if files:
                f  = files[0]
                dl = f.get("dlink") or f.get("download_url") or ""
                if dl:
                    logger.info("Worker API success ✅")
                    return {
                        "file_name":     f.get("filename") or "file",
                        "file_size":     int(f.get("size", 0)),
                        "download_link": dl,
                        "thumbnail":     f.get("thumb"),
                    }
    except Exception as e:
        errors.append(f"Worker: {e}")
        logger.warning("Worker fail: %s", e)

    # ── API 4: Worker fallback (no key needed) ──
    try:
        async with httpx.AsyncClient(timeout=25) as c:
            r = await c.post(
                "https://terabox.hnn.workers.dev/api/get-info",
                json={"url": url},
                headers={"User-Agent": "Mozilla/5.0"},
            )
            r.raise_for_status()
            data = r.json()

        if data.get("ok"):
            files = data.get("list", [])
            if files:
                f  = files[0]
                dl = f.get("dlink") or f.get("download_url") or f.get("url", "")
                if dl:
                    logger.info("Worker API success ✅")
                    return {
                        "file_name":     f.get("filename") or "file",
                        "file_size":     int(f.get("size", 0)),
                        "download_link": dl,
                        "thumbnail":     f.get("thumb"),
                    }
        errors.append(f"Worker: {data.get('message')}")
    except Exception as e:
        errors.append(f"Worker: {e}")
        logger.warning("Worker fail: %s", e)

    raise ValueError(
        "❌ Terabox se file nahi mili.\n\n"
        "Possible reasons:\n"
        "• Link private/expire ho gaya\n"
        "• Terabox server down hai\n\n"
        "Thodi der baad dobara try karein."
    )


def _parse_rapid(data):
    """RapidAPI response se download link nikalo — har format handle karo."""
    if not data:
        return None

    # List format
    if isinstance(data, list):
        data = data[0] if data else {}

    # Nested 'response' key
    if isinstance(data, dict) and "response" in data:
        inner = data["response"]
        if isinstance(inner, list) and inner:
            data = inner[0]
        elif isinstance(inner, dict):
            data = inner

    if not isinstance(data, dict):
        return None

    # Download link dhundo
    dl = (
        data.get("url") or
        data.get("download_url") or
        data.get("dlink") or
        data.get("download_link") or
        data.get("fast_download_link") or
        data.get("hd_mp4_url") or
        data.get("mp4_url") or
        ""
    )

    # resolutions ke andar dhundo
    if not dl:
        res = data.get("resolutions") or data.get("resolution") or {}
        if isinstance(res, dict):
            dl = (
                res.get("Fast Download") or
                res.get("HD Video") or
                res.get("SD Video") or
                res.get("Auto") or
                next(iter(res.values()), "")
            )

    if not dl or not str(dl).startswith("http"):
        return None

    return {
        "file_name":     (data.get("title") or data.get("file_name") or
                         data.get("name") or data.get("filename") or "video.mp4"),
        "file_size":     int(data.get("size", 0)),
        "download_link": dl,
        "thumbnail":     data.get("thumbnail") or data.get("thumb") or data.get("cover"),
    }

# ── Bot Handlers ─────────────────────────────────────────────────────────────

HELP_TEXT = (
    "🤖 *TeraBox Downloader Bot*\n\n"
    "Terabox link paste karo — file/video mil jaayegi!\n\n"
    f"🎁 Pehle *{FREE_LINKS}* links free hain\n"
    "Uske baad ek chhoti ad dekhni hogi\n\n"
    "/status — apna quota dekho"
)


async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    name = update.effective_user.first_name or "Dost"
    user = get_user(update.effective_user.id)
    gate, _ = check_gate(user)
    left = max(0, FREE_LINKS - user["link_count"]) if gate == Gate.ALLOW else FREE_LINKS
    await update.message.reply_text(
        f"Aao *{name}*! 👋\n\n{HELP_TEXT}\n\n"
        f"📊 Abhi *{left}* free link(s) baaki hain.",
        parse_mode=ParseMode.MARKDOWN,
    )


async def cmd_help(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(HELP_TEXT, parse_mode=ParseMode.MARKDOWN)


async def cmd_status(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    user = get_user(update.effective_user.id)
    gate, remaining = check_gate(user)
    if gate == Gate.COOLDOWN:
        txt = f"⏳ *Cooldown:* `{fmt_td(remaining)}` baad {FREE_LINKS} links milenge."
    elif gate == Gate.SHOW_AD:
        txt = "📢 Agli link bhejein — ad link milega, kholo, file milegi!"
    else:
        left = FREE_LINKS - user["link_count"]
        txt  = f"✅ *{left}* free link(s) baaki hain!"
    await update.message.reply_text(txt, parse_mode=ParseMode.MARKDOWN)


async def handle_link(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    msg     = update.message
    text    = (msg.text or "").strip()
    user_id = update.effective_user.id

    if not is_terabox(text):
        await msg.reply_text(
            "❓ Yeh Terabox link nahi lag raha.\n"
            "Valid URL paste karein (e.g. https://1024terabox.com/s/...)"
        )
        return

    user = get_user(user_id)
    gate, remaining = check_gate(user)

    if gate == Gate.COOLDOWN:
        await msg.reply_text(
            f"⏳ *Ruko!* `{fmt_td(remaining)}` baad access milega.",
            parse_mode=ParseMode.MARKDOWN,
        )
        return

    if gate == Gate.SHOW_AD:
        wait = await msg.reply_text("🔗 Ad link bana raha hoon...")
        ad   = await gplink_shorten(text)
        set_ad_shown(user)
        body = (
            f"📢 *{FREE_LINKS} free links use ho gaye!*\n\n"
            "👇 Button dabao → ad dekho → file pao\n\n"
            f"✅ Ad ke baad *{COOLDOWN_HRS} ghante* mein phir *{FREE_LINKS} free links*!"
        ) if GPLINK_API else "Seedha link:"
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("🎬 Ad Dekho & File Pao", url=ad)],
            [InlineKeyboardButton("📊 Mera Status", callback_data="status")],
        ])
        await wait.edit_text(body, parse_mode=ParseMode.MARKDOWN, reply_markup=kb)
        return

    sm = await msg.reply_text("⏳ Link check ho rahi hai...")

    try:
        info = await fetch_info(text)
    except httpx.TimeoutException:
        await sm.edit_text(
            "⏰ *Timeout!* Terabox slow hai. Dobara try karein.",
            parse_mode=ParseMode.MARKDOWN,
        )
        return
    except ValueError as e:
        await sm.edit_text(str(e), parse_mode=ParseMode.MARKDOWN)
        return
    except Exception:
        logger.error(traceback.format_exc())
        await sm.edit_text("💥 Unexpected error. Dobara try karein.")
        return

    increment_count(user)
    fresh = get_user(user_id)
    left  = max(0, FREE_LINKS - fresh["link_count"])

    fname   = info["file_name"]
    fsize   = info["file_size"]
    dl      = info["download_link"]
    caption = f"📁 *{fname}*\n📦 Size: `{human_size(fsize) if fsize else '?'}`"
    caption += (
        f"\n\n✅ *{left}* free link(s) aur baaki"
        if left > 0
        else "\n\n⚠️ Agli baar ad dekhna hoga"
    )

    if fsize and fsize > TELEGRAM_MAX_BYTES:
        kb = InlineKeyboardMarkup([[InlineKeyboardButton("⬇️ Direct Download", url=dl)]])
        await sm.edit_text(
            f"{caption}\n\n⚠️ *50 MB se badi file* — seedha download karo:",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=kb,
        )
        return

    note = "Badi file, thoda wait..." if fsize and fsize > WARN_SIZE_BYTES else "File aa rahi hai..."
    await sm.edit_text(f"{caption}\n\n📤 {note}", parse_mode=ParseMode.MARKDOWN)

    try:
        await _send_file(update, ctx, info, caption)
        await sm.delete()
    except _Big:
        kb = InlineKeyboardMarkup([[InlineKeyboardButton("⬇️ Direct Download", url=dl)]])
        await sm.edit_text(
            f"{caption}\n\n⚠️ Telegram ne reject kiya.",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=kb,
        )
    except Exception:
        logger.error(traceback.format_exc())
        kb = InlineKeyboardMarkup([[InlineKeyboardButton("⬇️ Direct Download", url=dl)]])
        await sm.edit_text(
            f"{caption}\n\n😓 Send nahi hua, direct link lo:",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=kb,
        )


async def handle_cb(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    if q.data == "status":
        await cmd_status(update, ctx)


class _Big(Exception):
    pass


async def _send_file(update, ctx, info, caption):
    dl    = info["download_link"]
    fname = info["file_name"]
    cid   = update.effective_chat.id
    if not dl:
        raise ValueError("Download link empty")

    ext      = Path(fname).suffix.lower()
    is_video = ext in (".mp4", ".mkv", ".avi", ".mov", ".webm", ".flv", ".m4v")

    with tempfile.NamedTemporaryFile(delete=False, suffix=ext or ".bin") as tmp:
        path = tmp.name

    try:
        async with httpx.AsyncClient(
            timeout=300,
            follow_redirects=True,
            headers={"User-Agent": "Mozilla/5.0"},
        ) as c:
            async with c.stream("GET", dl) as r:
                r.raise_for_status()
                done = 0
                with open(path, "wb") as f:
                    async for chunk in r.aiter_bytes(256 * 1024):
                        done += len(chunk)
                        if done > TELEGRAM_MAX_BYTES:
                            raise _Big()
                        f.write(chunk)

        with open(path, "rb") as f:
            kw = dict(
                chat_id=cid,
                filename=fname,
                caption=caption,
                parse_mode=ParseMode.MARKDOWN,
                read_timeout=120,
                write_timeout=120,
            )
            if is_video:
                await ctx.bot.send_video(video=f, supports_streaming=True, **kw)
            else:
                await ctx.bot.send_document(document=f, **kw)
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass

# ── Self ping ────────────────────────────────────────────────────────────────

async def ping_loop(url, interval=840):
    await asyncio.sleep(30)
    async with httpx.AsyncClient(timeout=10) as c:
        while True:
            try:
                r = await c.get(f"{url}/health")
                logger.info("Ping: %s", r.status_code)
            except Exception as e:
                logger.warning("Ping fail: %s", e)
            await asyncio.sleep(interval)

# ── App ──────────────────────────────────────────────────────────────────────

def build_ptb():
    app = Application.builder().token(BOT_TOKEN).read_timeout(60).write_timeout(120).build()
    app.add_handler(CommandHandler("start",  cmd_start))
    app.add_handler(CommandHandler("help",   cmd_help))
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CallbackQueryHandler(handle_cb))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_link))
    return app


def create_app():
    init_db()
    ptb  = build_ptb()
    fast = FastAPI(title="TeraBox Bot")

    @fast.on_event("startup")
    async def startup():
        await ptb.initialize()
        if WEBHOOK_URL:
            wh = f"{WEBHOOK_URL.rstrip('/')}/webhook"
            await ptb.bot.set_webhook(
                url=wh,
                allowed_updates=Update.ALL_TYPES,
                drop_pending_updates=True,
            )
            logger.info("Webhook: %s", wh)
            asyncio.create_task(ping_loop(WEBHOOK_URL.rstrip("/")))
        else:
            logger.warning("WEBHOOK_URL nahi — polling mode use karo")
        await ptb.start()

    @fast.on_event("shutdown")
    async def shutdown():
        await ptb.stop()
        await ptb.shutdown()

    @fast.post("/webhook")
    async def webhook(req: Request):
        data = await req.json()
        await ptb.process_update(Update.de_json(data=data, bot=ptb.bot))
        return Response(status_code=200)

    @fast.get("/health")
    async def health():
        me = await ptb.bot.get_me()
        return {
            "status":     "ok",
            "bot":        me.username,
            "free_links": FREE_LINKS,
            "rapidapi":   "set" if RAPIDAPI_KEY else "MISSING",
            "db":         "mongodb" if MONGO_URI else "memory",
        }

    return fast


def run_polling():
    init_db()
    build_ptb().run_polling(allowed_updates=Update.ALL_TYPES, drop_pending_updates=True)


def run_webhook():
    import uvicorn
    uvicorn.run(create_app(), host="0.0.0.0", port=PORT, log_level="info")


if __name__ == "__main__":
    import sys
    if "--polling" in sys.argv:
        run_polling()
    else:
        run_webhook()
