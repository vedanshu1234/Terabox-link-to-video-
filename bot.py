"""
TeraBox Telegram Bot — MongoDB + Bina Cookie ke Kaam Karta Hai
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

TELEGRAM_MAX_BYTES = 50 * 1024 * 1024
WARN_SIZE_BYTES    = 45 * 1024 * 1024

if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN set nahi hai!")

# ── MongoDB ────────────────────────────────────────────────────────────────────

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
                self._d[uid] = {"user_id": uid, "link_count": 0, "ad_shown_at": None, "reset_at": None}
            else:
                return
        self._d[uid].update(upd.get("$set", {}))


def init_db():
    col = get_col()
    if MONGO_URI:
        col.find_one({"user_id": 0})
        logger.info("MongoDB ready")


def get_user(user_id: int) -> dict:
    doc = get_col().find_one({"user_id": user_id})
    if doc is None:
        return {"user_id": user_id, "link_count": 0, "ad_shown_at": None, "reset_at": None}
    doc.pop("_id", None)
    return doc


def save_user(data: dict):
    d = {k: v for k, v in data.items() if k != "_id"}
    get_col().update_one({"user_id": data["user_id"]}, {"$set": d}, upsert=True)

# ── Gate logic ─────────────────────────────────────────────────────────────────

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

# ── GPLink ─────────────────────────────────────────────────────────────────────

async def gplink_shorten(url):
    if not GPLINK_API:
        return url
    try:
        async with httpx.AsyncClient(timeout=10) as c:
            r = await c.get(f"https://api.gplinks.com/api?api={GPLINK_API}&url={url}&format=text")
            r.raise_for_status()
            s = r.text.strip()
            if s.startswith("http"):
                return s
    except Exception as e:
        logger.error("GPLink error: %s", e)
    return url

# ── Terabox ────────────────────────────────────────────────────────────────────

TERABOX_DOMAINS = (
    "terabox.com", "1024terabox.com", "terabox.app",
    "teraboxapp.com", "4funbox.com", "mirrobox.com",
    "nephobox.com", "freeterabox.com", "momerybox.com",
    "tibibox.com", "sendcm.com",
)


def is_terabox(text):
    return any(d in text.lower() for d in TERABOX_DOMAINS)


def human_size(n):
    for u in ("B", "KB", "MB", "GB"):
        if abs(n) < 1024:
            return f"{n:.1f} {u}"
        n /= 1024
    return f"{n:.1f} TB"


async def fetch_info(url):
    # Cookie hai toh TeraboxDL try karo
    if COOKIE:
        try:
            from TeraboxDL import TeraboxDL
            tb   = TeraboxDL(cookie=COOKIE)
            info = await asyncio.to_thread(tb.get_file_info, url, direct_url=True)
            if "error" not in info:
                return {
                    "file_name":     info.get("file_name", "file"),
                    "file_size":     int(info.get("file_size", 0)),
                    "download_link": info.get("download_link") or info.get("direct_url", ""),
                    "thumbnail":     info.get("thumbnail"),
                }
        except Exception as e:
            logger.warning("TeraboxDL fail: %s — fallback chalega", e)

    # Fallback: public API — bina cookie ke kaam karta hai
    return await _fallback(url)


async def _fallback(url):
    async with httpx.AsyncClient(timeout=20) as c:
        r = await c.post("https://terabox.hnn.workers.dev/api/get-info", json={"url": url})
        r.raise_for_status()
        data = r.json()
    if not data.get("ok"):
        raise ValueError(data.get("message") or "Terabox se data nahi mila")
    files = data.get("list", [])
    if not files:
        raise ValueError("Is link mein koi file nahi mili")
    f = files[0]
    return {
        "file_name":     f.get("filename", "file"),
        "file_size":     int(f.get("size", 0)),
        "download_link": f.get("dlink") or f.get("download_url", ""),
        "thumbnail":     f.get("thumb"),
    }

# ── Handlers ───────────────────────────────────────────────────────────────────

HELP_TEXT = (
    "🤖 *TeraBox Downloader Bot*\n\n"
    "Terabox link paste karo — file/video mil jaayegi!\n\n"
    f"🎁 Pehle *{FREE_LINKS}* links free hain\n"
    "Uske baad ek ad dekhni hogi\n\n"
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
    user  = get_user(update.effective_user.id)
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
        ) if GPLINK_API else "⚠️ GPLink set nahi. Seedha link:"
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
        await sm.edit_text("⏰ *Timeout!* Terabox slow hai. Dobara try karein.", parse_mode=ParseMode.MARKDOWN)
        return
    except httpx.HTTPStatusError as e:
        await sm.edit_text(f"🌐 *HTTP {e.response.status_code}* error. Baad mein try karein.", parse_mode=ParseMode.MARKDOWN)
        return
    except ValueError as e:
        await sm.edit_text(f"❌ *Link fail:* {e}", parse_mode=ParseMode.MARKDOWN)
        return
    except Exception:
        logger.error(traceback.format_exc())
        await sm.edit_text("💥 Kuch galat ho gaya. Dobara try karein.")
        return

    increment_count(user)
    fresh = get_user(user_id)
    left  = max(0, FREE_LINKS - fresh["link_count"])

    fname   = info["file_name"]
    fsize   = info["file_size"]
    dl      = info["download_link"]
    caption = f"📁 *{fname}*\n📦 Size: `{human_size(fsize) if fsize else '?'}`"
    caption += f"\n\n✅ *{left}* free link(s) aur baaki" if left > 0 else "\n\n⚠️ Agli baar ad dekhna hoga"

    if fsize and fsize > TELEGRAM_MAX_BYTES:
        kb = InlineKeyboardMarkup([[InlineKeyboardButton("⬇️ Direct Download", url=dl)]])
        await sm.edit_text(
            f"{caption}\n\n⚠️ *50 MB se badi file* — seedha download karo:",
            parse_mode=ParseMode.MARKDOWN, reply_markup=kb,
        )
        return

    note = "Badi file, thoda wait..." if fsize and fsize > WARN_SIZE_BYTES else "File aa rahi hai..."
    await sm.edit_text(f"{caption}\n\n📤 {note}", parse_mode=ParseMode.MARKDOWN)

    try:
        await _send_file(update, ctx, info, caption)
        await sm.delete()
    except _Big:
        kb = InlineKeyboardMarkup([[InlineKeyboardButton("⬇️ Direct Download", url=dl)]])
        await sm.edit_text(f"{caption}\n\n⚠️ Telegram ne reject kiya.", parse_mode=ParseMode.MARKDOWN, reply_markup=kb)
    except Exception:
        logger.error(traceback.format_exc())
        kb = InlineKeyboardMarkup([[InlineKeyboardButton("⬇️ Direct Download", url=dl)]])
        await sm.edit_text(f"{caption}\n\n😓 Send nahi hua, link lo:", parse_mode=ParseMode.MARKDOWN, reply_markup=kb)


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
        async with httpx.AsyncClient(timeout=300, follow_redirects=True) as c:
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
            kw = dict(chat_id=cid, filename=fname, caption=caption,
                      parse_mode=ParseMode.MARKDOWN, read_timeout=120, write_timeout=120)
            if is_video:
                await ctx.bot.send_video(video=f, supports_streaming=True, **kw)
            else:
                await ctx.bot.send_document(document=f, **kw)
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass

# ── Self ping ──────────────────────────────────────────────────────────────────

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

# ── App ────────────────────────────────────────────────────────────────────────

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
            await ptb.bot.set_webhook(url=wh, allowed_updates=Update.ALL_TYPES, drop_pending_updates=True)
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
        return {"status": "ok", "bot": me.username, "free_links": FREE_LINKS, "db": "mongodb" if MONGO_URI else "memory"}

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
TeraBox Telegram Bot — MongoDB + Bina Cookie ke Kaam Karta Hai
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

TELEGRAM_MAX_BYTES = 50 * 1024 * 1024
WARN_SIZE_BYTES    = 45 * 1024 * 1024

if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN set nahi hai!")

# ── MongoDB ────────────────────────────────────────────────────────────────────

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
                self._d[uid] = {"user_id": uid, "link_count": 0, "ad_shown_at": None, "reset_at": None}
            else:
                return
        self._d[uid].update(upd.get("$set", {}))


def init_db():
    col = get_col()
    if MONGO_URI:
        col.find_one({"user_id": 0})
        logger.info("MongoDB ready")


def get_user(user_id: int) -> dict:
    doc = get_col().find_one({"user_id": user_id})
    if doc is None:
        return {"user_id": user_id, "link_count": 0, "ad_shown_at": None, "reset_at": None}
    doc.pop("_id", None)
    return doc


def save_user(data: dict):
    d = {k: v for k, v in data.items() if k != "_id"}
    get_col().update_one({"user_id": data["user_id"]}, {"$set": d}, upsert=True)

# ── Gate logic ─────────────────────────────────────────────────────────────────

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

# ── GPLink ─────────────────────────────────────────────────────────────────────

async def gplink_shorten(url):
    if not GPLINK_API:
        return url
    try:
        async with httpx.AsyncClient(timeout=10) as c:
            r = await c.get(f"https://api.gplinks.com/api?api={GPLINK_API}&url={url}&format=text")
            r.raise_for_status()
            s = r.text.strip()
            if s.startswith("http"):
                return s
    except Exception as e:
        logger.error("GPLink error: %s", e)
    return url

# ── Terabox ────────────────────────────────────────────────────────────────────

TERABOX_DOMAINS = (
    "terabox.com", "1024terabox.com", "terabox.app",
    "teraboxapp.com", "4funbox.com", "mirrobox.com",
    "nephobox.com", "freeterabox.com", "momerybox.com",
    "tibibox.com", "sendcm.com",
)


def is_terabox(text):
    return any(d in text.lower() for d in TERABOX_DOMAINS)


def human_size(n):
    for u in ("B", "KB", "MB", "GB"):
        if abs(n) < 1024:
            return f"{n:.1f} {u}"
        n /= 1024
    return f"{n:.1f} TB"


async def fetch_info(url):
    # Cookie hai toh TeraboxDL try karo
    if COOKIE:
        try:
            from TeraboxDL import TeraboxDL
            tb   = TeraboxDL(cookie=COOKIE)
            info = await asyncio.to_thread(tb.get_file_info, url, direct_url=True)
            if "error" not in info:
                return {
                    "file_name":     info.get("file_name", "file"),
                    "file_size":     int(info.get("file_size", 0)),
                    "download_link": info.get("download_link") or info.get("direct_url", ""),
                    "thumbnail":     info.get("thumbnail"),
                }
        except Exception as e:
            logger.warning("TeraboxDL fail: %s — fallback chalega", e)

    # Fallback: public API — bina cookie ke kaam karta hai
    return await _fallback(url)


async def _fallback(url):
    async with httpx.AsyncClient(timeout=20) as c:
        r = await c.post("https://terabox.hnn.workers.dev/api/get-info", json={"url": url})
        r.raise_for_status()
        data = r.json()
    if not data.get("ok"):
        raise ValueError(data.get("message") or "Terabox se data nahi mila")
    files = data.get("list", [])
    if not files:
        raise ValueError("Is link mein koi file nahi mili")
    f = files[0]
    return {
        "file_name":     f.get("filename", "file"),
        "file_size":     int(f.get("size", 0)),
        "download_link": f.get("dlink") or f.get("download_url", ""),
        "thumbnail":     f.get("thumb"),
    }

# ── Handlers ───────────────────────────────────────────────────────────────────

HELP_TEXT = (
    "🤖 *TeraBox Downloader Bot*\n\n"
    "Terabox link paste karo — file/video mil jaayegi!\n\n"
    f"🎁 Pehle *{FREE_LINKS}* links free hain\n"
    "Uske baad ek ad dekhni hogi\n\n"
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
    user  = get_user(update.effective_user.id)
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
        ) if GPLINK_API else "⚠️ GPLink set nahi. Seedha link:"
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
        await sm.edit_text("⏰ *Timeout!* Terabox slow hai. Dobara try karein.", parse_mode=ParseMode.MARKDOWN)
        return
    except httpx.HTTPStatusError as e:
        await sm.edit_text(f"🌐 *HTTP {e.response.status_code}* error. Baad mein try karein.", parse_mode=ParseMode.MARKDOWN)
        return
    except ValueError as e:
        await sm.edit_text(f"❌ *Link fail:* {e}", parse_mode=ParseMode.MARKDOWN)
        return
    except Exception:
        logger.error(traceback.format_exc())
        await sm.edit_text("💥 Kuch galat ho gaya. Dobara try karein.")
        return

    increment_count(user)
    fresh = get_user(user_id)
    left  = max(0, FREE_LINKS - fresh["link_count"])

    fname   = info["file_name"]
    fsize   = info["file_size"]
    dl      = info["download_link"]
    caption = f"📁 *{fname}*\n📦 Size: `{human_size(fsize) if fsize else '?'}`"
    caption += f"\n\n✅ *{left}* free link(s) aur baaki" if left > 0 else "\n\n⚠️ Agli baar ad dekhna hoga"

    if fsize and fsize > TELEGRAM_MAX_BYTES:
        kb = InlineKeyboardMarkup([[InlineKeyboardButton("⬇️ Direct Download", url=dl)]])
        await sm.edit_text(
            f"{caption}\n\n⚠️ *50 MB se badi file* — seedha download karo:",
            parse_mode=ParseMode.MARKDOWN, reply_markup=kb,
        )
        return

    note = "Badi file, thoda wait..." if fsize and fsize > WARN_SIZE_BYTES else "File aa rahi hai..."
    await sm.edit_text(f"{caption}\n\n📤 {note}", parse_mode=ParseMode.MARKDOWN)

    try:
        await _send_file(update, ctx, info, caption)
        await sm.delete()
    except _Big:
        kb = InlineKeyboardMarkup([[InlineKeyboardButton("⬇️ Direct Download", url=dl)]])
        await sm.edit_text(f"{caption}\n\n⚠️ Telegram ne reject kiya.", parse_mode=ParseMode.MARKDOWN, reply_markup=kb)
    except Exception:
        logger.error(traceback.format_exc())
        kb = InlineKeyboardMarkup([[InlineKeyboardButton("⬇️ Direct Download", url=dl)]])
        await sm.edit_text(f"{caption}\n\n😓 Send nahi hua, link lo:", parse_mode=ParseMode.MARKDOWN, reply_markup=kb)


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
        async with httpx.AsyncClient(timeout=300, follow_redirects=True) as c:
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
            kw = dict(chat_id=cid, filename=fname, caption=caption,
                      parse_mode=ParseMode.MARKDOWN, read_timeout=120, write_timeout=120)
            if is_video:
                await ctx.bot.send_video(video=f, supports_streaming=True, **kw)
            else:
                await ctx.bot.send_document(document=f, **kw)
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass

# ── Self ping ──────────────────────────────────────────────────────────────────

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

# ── App ────────────────────────────────────────────────────────────────────────

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
            await ptb.bot.set_webhook(url=wh, allowed_updates=Update.ALL_TYPES, drop_pending_updates=True)
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
        return {"status": "ok", "bot": me.username, "free_links": FREE_LINKS, "db": "mongodb" if MONGO_URI else "memory"}

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
