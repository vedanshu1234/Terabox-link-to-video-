"""
TeraBox Telegram Bot — MongoDB Atlas ke saath
=============================================
Features:
  • Terabox link → file/video seedha Telegram mein
  • GPLink ad system: N free links → ad → phir N free links
  • MongoDB Atlas database (hamesha free!)
  • Self-ping: Render ke 15-min sleep se bachne ke liye
  • Webhook mode (production) + Polling mode (local test)

Environment variables (.env / Render dashboard mein):
  BOT_TOKEN      — BotFather se mila token              [ZARURI]
  MONGO_URI      — MongoDB Atlas connection string       [ZARURI]
  WEBHOOK_URL    — aapka public HTTPS domain             [ZARURI on Render]
  GPLINK_API     — GPLink developer API key              [ZARURI for ads]
  PORT           — server port (default 10000)
  COOKIE         — TeraBox account cookie                [optional]
  FREE_LINKS     — free links before ad (default 3)
  COOLDOWN_HRS   — hours after ad (default 7)
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

# ─── Config ───────────────────────────────────────────────────────────────────

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
    raise RuntimeError("BOT_TOKEN env var set nahi hai!")


# ─── MongoDB Database ─────────────────────────────────────────────────────────

_mongo_collection = None

def get_collection():
    """MongoDB collection return karo (lazy init, cached)."""
    global _mongo_collection
    if _mongo_collection is not None:
        return _mongo_collection

    if MONGO_URI:
        from pymongo import MongoClient
        client = MongoClient(MONGO_URI, serverSelectionTimeoutMS=5000)
        db = client["terabox_bot"]
        _mongo_collection = db["users"]
        logger.info("MongoDB Atlas connected ✅")
    else:
        # Local dev ke liye: mongomock (ya sirf dict in-memory)
        logger.warning("MONGO_URI nahi mila — in-memory storage use ho rahi hai (restart pe data jayega)")
        _mongo_collection = _InMemoryStore()

    return _mongo_collection


class _InMemoryStore:
    """Local testing ke liye: MongoDB jaisi API, data RAM mein."""
    def __init__(self):
        self._data = {}

    def find_one(self, query):
        uid = query.get("user_id")
        return self._data.get(uid)

    def update_one(self, query, update, upsert=False):
        uid = query.get("user_id")
        if uid not in self._data:
            if upsert:
                self._data[uid] = {"user_id": uid, "link_count": 0,
                                   "ad_shown_at": None, "reset_at": None}
            else:
                return
        set_data = update.get("$set", {})
        self._data[uid].update(set_data)


def init_db() -> None:
    """DB connection test karo startup pe."""
    col = get_collection()
    if MONGO_URI:
        try:
            # Ping karo
            col.find_one({"user_id": 0})
            logger.info("MongoDB ready ✅")
        except Exception as e:
            logger.error("MongoDB connection fail: %s", e)
            raise


def get_user(user_id: int) -> dict:
    """User ka record fetch karo, nahi hai toh default return karo."""
    col = get_collection()
    doc = col.find_one({"user_id": user_id})
    if doc is None:
        return {
            "user_id":     user_id,
            "link_count":  0,
            "ad_shown_at": None,
            "reset_at":    None,
        }
    # MongoDB _id field hata do
    doc.pop("_id", None)
    return doc


def save_user(data: dict) -> None:
    """User record save/update karo."""
    col = get_collection()
    save_data = {k: v for k, v in data.items() if k != "_id"}
    col.update_one(
        {"user_id": data["user_id"]},
        {"$set": save_data},
        upsert=True,
    )


# ─── User gate logic ──────────────────────────────────────────────────────────

def _now() -> datetime:
    return datetime.now(timezone.utc)


def _parse_ts(ts) -> datetime | None:
    if ts is None:
        return None
    if isinstance(ts, datetime):
        return ts.replace(tzinfo=timezone.utc) if ts.tzinfo is None else ts
    return datetime.fromisoformat(str(ts))


class Gate:
    ALLOW    = "allow"
    SHOW_AD  = "show_ad"
    COOLDOWN = "cooldown"


def check_gate(user: dict) -> tuple[str, timedelta | None]:
    now      = _now()
    reset_at = _parse_ts(user.get("reset_at"))

    # Cooldown chal rahi hai?
    if reset_at and now < reset_at:
        return Gate.COOLDOWN, reset_at - now

    # Reset time guzar gayi → counter clear
    if reset_at and now >= reset_at:
        user["link_count"]  = 0
        user["reset_at"]    = None
        user["ad_shown_at"] = None

    if user["link_count"] < FREE_LINKS:
        return Gate.ALLOW, None

    return Gate.SHOW_AD, None


def increment_count(user: dict) -> None:
    user["link_count"] += 1
    save_user(user)


def set_ad_shown(user: dict) -> None:
    now = _now()
    user["ad_shown_at"] = now.isoformat()
    user["reset_at"]    = (now + timedelta(hours=COOLDOWN_HRS)).isoformat()
    save_user(user)


def format_td(td: timedelta) -> str:
    total = int(td.total_seconds())
    hrs   = total // 3600
    mins  = (total % 3600) // 60
    if hrs > 0 and mins > 0:
        return f"{hrs} ghante {mins} minute"
    if hrs > 0:
        return f"{hrs} ghante"
    return f"{mins} minute"


# ─── GPLink ───────────────────────────────────────────────────────────────────

async def gplink_shorten(url: str) -> str:
    if not GPLINK_API:
        return url
    api = f"https://api.gplinks.com/api?api={GPLINK_API}&url={url}&format=text"
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.get(api)
            r.raise_for_status()
            short = r.text.strip()
            if short.startswith("http"):
                return short
    except Exception as e:
        logger.error("GPLink error: %s", e)
    return url


# ─── Terabox ──────────────────────────────────────────────────────────────────

TERABOX_DOMAINS = (
    "terabox.com", "1024terabox.com", "terabox.app",
    "teraboxapp.com", "4funbox.com", "mirrobox.com",
    "nephobox.com", "freeterabox.com", "momerybox.com",
    "tibibox.com", "sendcm.com",
)


def is_terabox_url(text: str) -> bool:
    return any(d in text.lower() for d in TERABOX_DOMAINS)


def human_size(n: int) -> str:
    for u in ("B", "KB", "MB", "GB"):
        if abs(n) < 1024:
            return f"{n:.1f} {u}"
        n /= 1024
    return f"{n:.1f} TB"


async def fetch_terabox_info(url: str) -> dict:
    try:
        from TeraboxDL import TeraboxDL  # type: ignore
        tb   = TeraboxDL(cookie=COOKIE or None)
        info = await asyncio.to_thread(tb.get_file_info, url, direct_url=True)
        if "error" in info:
            raise ValueError(info["error"])
        return {
            "file_name":     info.get("file_name", "file"),
            "file_size":     int(info.get("file_size", 0)),
            "download_link": info.get("download_link") or info.get("direct_url", ""),
            "thumbnail":     info.get("thumbnail"),
        }
    except ImportError:
        return await _fallback_api(url)


async def _fallback_api(url: str) -> dict:
    async with httpx.AsyncClient(timeout=20) as client:
        r = await client.post(
            "https://terabox.hnn.workers.dev/api/get-info",
            json={"url": url},
        )
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


# ─── Bot handlers ─────────────────────────────────────────────────────────────

HELP_TEXT = f"""
🤖 *TeraBox Downloader Bot*

📋 *Kaise use karein:*
Koi bhi Terabox link yahan paste karein

🎁 *Free system:*
• Pehle *{FREE_LINKS} links* — bilkul free, seedha file
• Uske baad — ek chhoti si ad dekhni hogi
• Ad ke baad — agli *{COOLDOWN_HRS} ghante* mein phir *{FREE_LINKS} free links*

📦 *File size limit:* 50 MB (Telegram ki limit)
Badi files ke liye direct link milega 🔗

📊 Apna quota dekhne ke liye: /status
""".strip()


async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    name = update.effective_user.first_name or "Dost"
    user = get_user(update.effective_user.id)
    gate, _ = check_gate(user)
    left = max(0, FREE_LINKS - user["link_count"]) if gate == Gate.ALLOW else FREE_LINKS
    await update.message.reply_text(
        f"Aao *{name}*! 👋\n\n{HELP_TEXT}\n\n"
        f"📊 Abhi *{left}* free link(s) baaki hain.",
        parse_mode=ParseMode.MARKDOWN,
    )


async def cmd_help(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(HELP_TEXT, parse_mode=ParseMode.MARKDOWN)


async def cmd_status(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    user  = get_user(update.effective_user.id)
    gate, remaining = check_gate(user)

    if gate == Gate.COOLDOWN:
        t   = format_td(remaining)
        txt = (
            f"⏳ *Cooldown chal raha hai*\n\n"
            f"`{t}` baad phir se {FREE_LINKS} free links milenge.\n"
            f"Ya phir abhi ad dekho aur turant unlock karo! 🔓"
        )
    elif gate == Gate.SHOW_AD:
        txt = (
            f"📢 *Ad ka time hai!*\n\n"
            f"Agli link bhejein → ad link milega → kholo → file milegi\n"
            f"Phir {COOLDOWN_HRS} ghante mein {FREE_LINKS} fresh links!"
        )
    else:
        left = FREE_LINKS - user["link_count"]
        txt  = f"✅ *{left}* free link(s) baaki hain!\nKoi bhi Terabox link bhejein."

    await update.message.reply_text(txt, parse_mode=ParseMode.MARKDOWN)


async def handle_link(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    msg     = update.message
    text    = (msg.text or "").strip()
    user_id = update.effective_user.id

    if not is_terabox_url(text):
        await msg.reply_text(
            "❓ Yeh Terabox link nahi lag raha.\n"
            "Valid URL paste karein (e.g. https://1024terabox.com/s/...)"
        )
        return

    user = get_user(user_id)
    gate, remaining = check_gate(user)

    # ── COOLDOWN ──────────────────────────────────────────────────────────────
    if gate == Gate.COOLDOWN:
        t = format_td(remaining)
        await msg.reply_text(
            f"⏳ *Ruko thoda!*\n\n`{t}` baad access milega.\n"
            f"Ya /status se time check karo.",
            parse_mode=ParseMode.MARKDOWN,
        )
        return

    # ── AD REQUIRED ───────────────────────────────────────────────────────────
    if gate == Gate.SHOW_AD:
        wait_msg = await msg.reply_text("🔗 Ad link bana raha hoon...")
        ad_link  = await gplink_shorten(text)
        set_ad_shown(user)

        if GPLINK_API:
            body = (
                f"📢 *{FREE_LINKS} free links use ho gaye!*\n\n"
                "👇 Neeche button dabao → ad page khulega\n"
                "Kuch second baad file mil jaayegi\n\n"
                f"✅ Ad ke baad *{COOLDOWN_HRS} ghante* mein\n"
                f"phir se *{FREE_LINKS} free links* milenge!"
            )
        else:
            body = (
                "⚠️ _GPLink API set nahi (admin se baat karein)_\n\n"
                "Abhi ke liye seedha link:"
            )

        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("🎬 Ad Dekho & File Pao", url=ad_link)],
            [InlineKeyboardButton("📊 Mera Status", callback_data="status")],
        ])
        await wait_msg.edit_text(body, parse_mode=ParseMode.MARKDOWN, reply_markup=kb)
        return

    # ── FREE: file fetch karo ─────────────────────────────────────────────────
    status_msg = await msg.reply_text("⏳ Link check ho rahi hai...")

    try:
        info = await fetch_terabox_info(text)
    except httpx.TimeoutException:
        await status_msg.edit_text(
            "⏰ *Timeout!* Terabox slow hai. Dobara try karein.",
            parse_mode=ParseMode.MARKDOWN,
        )
        return
    except httpx.HTTPStatusError as e:
        await status_msg.edit_text(
            f"🌐 *HTTP {e.response.status_code}* — server problem. Baad mein try karein.",
            parse_mode=ParseMode.MARKDOWN,
        )
        return
    except ValueError as e:
        await status_msg.edit_text(
            f"❌ *Link fail:* {e}\n\n_Expire/private ho sakta hai._",
            parse_mode=ParseMode.MARKDOWN,
        )
        return
    except Exception:
        logger.error(traceback.format_exc())
        await status_msg.edit_text("💥 Kuch galat ho gaya. Dobara try karein.")
        return

    # Counter badhao
    increment_count(user)
    user_fresh = get_user(user_id)
    left = max(0, FREE_LINKS - user_fresh["link_count"])

    fname    = info["file_name"]
    fsize    = info["file_size"]
    dl_link  = info["download_link"]
    size_str = human_size(fsize) if fsize else "?"
    caption  = f"📁 *{fname}*\n📦 Size: `{size_str}`"

    if left > 0:
        caption += f"\n\n✅ *{left}* free link(s) aur baaki"
    else:
        caption += f"\n\n⚠️ Agli baar ad dekhna hoga"

    # 50 MB se badi file
    if fsize and fsize > TELEGRAM_MAX_BYTES:
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("⬇️ Direct Download", url=dl_link)]
        ])
        await status_msg.edit_text(
            f"{caption}\n\n⚠️ *File 50 MB se badi hai* — Telegram seedha nahi bhej sakta.",
            parse_mode=ParseMode.MARKDOWN, reply_markup=kb,
        )
        return

    await status_msg.edit_text(
        f"{caption}\n\n📤 "
        f"{'Badi file, thoda wait...' if fsize and fsize > WARN_SIZE_BYTES else 'File aa rahi hai...'}",
        parse_mode=ParseMode.MARKDOWN,
    )

    try:
        await _download_and_send(update, ctx, info, caption)
        await status_msg.delete()
    except _FileTooLarge:
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("⬇️ Direct Download", url=dl_link)]
        ])
        await status_msg.edit_text(
            f"{caption}\n\n⚠️ Telegram ne reject kiya (size limit).",
            parse_mode=ParseMode.MARKDOWN, reply_markup=kb,
        )
    except Exception:
        logger.error(traceback.format_exc())
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("⬇️ Direct Download", url=dl_link)]
        ])
        await status_msg.edit_text(
            f"{caption}\n\n😓 Send nahi hua, link yahan hai:",
            parse_mode=ParseMode.MARKDOWN, reply_markup=kb,
        )


async def handle_callback(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    await q.answer()
    if q.data == "status":
        await cmd_status(update, ctx)


class _FileTooLarge(Exception):
    pass


async def _download_and_send(update, ctx, info, caption):
    dl    = info["download_link"]
    fname = info["file_name"]
    cid   = update.effective_chat.id
    if not dl:
        raise ValueError("Empty download link")

    ext      = Path(fname).suffix.lower()
    is_video = ext in (".mp4", ".mkv", ".avi", ".mov", ".webm", ".flv", ".m4v")

    with tempfile.NamedTemporaryFile(delete=False, suffix=ext or ".bin") as tmp:
        tmp_path = tmp.name

    try:
        async with httpx.AsyncClient(timeout=300, follow_redirects=True) as client:
            async with client.stream("GET", dl) as r:
                r.raise_for_status()
                done = 0
                with open(tmp_path, "wb") as f:
                    async for chunk in r.aiter_bytes(256 * 1024):
                        done += len(chunk)
                        if done > TELEGRAM_MAX_BYTES:
                            raise _FileTooLarge()
                        f.write(chunk)

        with open(tmp_path, "rb") as f:
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
            os.unlink(tmp_path)
        except OSError:
            pass


# ─── Self-ping (Render free tier ke liye) ────────────────────────────────────

async def self_ping_loop(url: str, interval: int = 840) -> None:
    """Har 14 minute mein ping — Render ko jagte rakhta hai."""
    await asyncio.sleep(30)
    async with httpx.AsyncClient(timeout=10) as client:
        while True:
            try:
                r = await client.get(f"{url}/health")
                logger.info("Self-ping: %s", r.status_code)
            except Exception as e:
                logger.warning("Self-ping fail: %s", e)
            await asyncio.sleep(interval)


# ─── App setup ────────────────────────────────────────────────────────────────

def build_ptb() -> Application:
    app = (
        Application.builder()
        .token(BOT_TOKEN)
        .read_timeout(60)
        .write_timeout(120)
        .build()
    )
    app.add_handler(CommandHandler("start",  cmd_start))
    app.add_handler(CommandHandler("help",   cmd_help))
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CallbackQueryHandler(handle_callback))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_link))
    return app


def create_app() -> FastAPI:
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
            logger.info("Webhook set: %s", wh)
            asyncio.create_task(self_ping_loop(WEBHOOK_URL.rstrip("/")))
        else:
            logger.warning("WEBHOOK_URL nahi — polling chalao")
        await ptb.start()

    @fast.on_event("shutdown")
    async def shutdown():
        await ptb.stop()
        await ptb.shutdown()

    @fast.post("/webhook")
    async def webhook(request: Request):
        data   = await request.json()
        update = Update.de_json(data=data, bot=ptb.bot)
        await ptb.process_update(update)
        return Response(status_code=200)

    @fast.get("/health")
    async def health():
        me = await ptb.bot.get_me()
        return {
            "status":       "ok",
            "bot":          me.username,
            "free_links":   FREE_LINKS,
            "cooldown_hrs": COOLDOWN_HRS,
            "db":           "mongodb" if MONGO_URI else "in-memory",
        }

    return fast


# ─── Entry points ─────────────────────────────────────────────────────────────

def run_polling():
    init_db()
    logger.info("Polling mode...")
    ptb = build_ptb()
    ptb.run_polling(allowed_updates=Update.ALL_TYPES, drop_pending_updates=True)


def run_webhook():
    import uvicorn
    fast = create_app()
    uvicorn.run(fast, host="0.0.0.0", port=PORT, log_level="info")


if __name__ == "__main__":
    import sys
    if "--polling" in sys.argv:
        run_polling()
    else:
        run_webhook()
