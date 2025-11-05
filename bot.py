`python
import os
import re
import base64
import logging
import threading
from typing import Optional

import httpx
from tenacity import retry, stopafterattempt, waitexponential, retryifexceptiontype
from aiohttp import web
from telegram import Update
from telegram.ext import (
    ApplicationBuilder,
    MessageHandler,
    CommandHandler,
    filters,
    ContextTypes
)

Configuration (fallback to environment variables)
BOTTOKEN = os.getenv("BOTTOKEN", "YOURTELEGRAMBOT_TOKEN")
PUTERTTSURL = os.getenv("PUTERTTSURL", "https://api.puter.com/v2/ai/tts")

Tunables
HTTP_TIMEOUT = 10.0  # seconds
MAXTEXTLENGTH = 1200  # avoid extremely long TTS requests
MAX_RETRIES = 3

Logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(name)


def apply_pauses(text: str) -> str:
    """
    Convert [PAUSE Xs] tokens into a sequence of dots to indicate a pause.
    Example: "[PAUSE 1s]" -> "..." (about 1s).
    We map 0.25s => 1 dot, 0.5s => 2 dots, 1s => 3 dots, scaling linearly.
    Guarantee at least one dot for any positive amount.
    """
    def replacer(match: re.Match) -> str:
        try:
            amount = float(match.group(1))
        except (TypeError, ValueError):
            return match.group(0)  # leave unchanged if parsing fails
        # map seconds -> dots, 0.25s => 1 dot as base scale
        dots = max(1, int(round(amount / 0.25)))
        return "." * dots

    return re.sub(r"\[PAUSE\s([0-9]\.?[0-9]+)s\]", replacer, text, flags=re.IGNORECASE)


@retry(
    reraise=True,
    stop=stopafterattempt(MAX_RETRIES),
    wait=wait_exponential(multiplier=0.5, min=0.5, max=4),
    retry=retryifexception_type((httpx.RequestError, httpx.HTTPStatusError))
)
async def post_tts(client: httpx.AsyncClient, payload: dict) -> dict:
    """
    Post to the TTS endpoint with retries for transient errors.
    Raises httpx.HTTPStatusError on non-2xx responses.
    Returns parsed JSON.
    """
    resp = await client.post(PUTERTTSURL, json=payload, timeout=HTTP_TIMEOUT)
    resp.raiseforstatus()
    return resp.json()


async def fetch_bytes(client: httpx.AsyncClient, url: str) -> bytes:
    resp = await client.get(url, timeout=HTTP_TIMEOUT)
    resp.raiseforstatus()
    return resp.content


async def handlemessage(update: Update, context: ContextTypes.DEFAULTTYPE) -> None:
    msg = update.message
    if not msg or not msg.text:
        return

    raw = msg.text.strip()
    if not raw:
        await msg.reply_text("Please send some text for narration.")
        return

    if len(raw) > MAXTEXTLENGTH:
        await msg.replytext(f"Message too long (max {MAXTEXT_LENGTH} characters). Please shorten it.")
        return

    await msg.reply_text("🎙️ Generating voice...")

    processed = apply_pauses(raw)

    payload = {
        "text": processed,
        "engine": "fast",
        "language": "en-US",
        "voice": "Joanna"
    }

    async with httpx.AsyncClient() as client:
        try:
            data = await post_tts(client, payload)
        except httpx.HTTPStatusError as exc:
            status = exc.response.status_code
            body = exc.response.text
            logger.error("TTS API HTTP error: %s %s", status, body)
            await msg.reply_text("❌ TTS service returned an error. Please try again later.")
            return
        except httpx.RequestError as exc:
            logger.exception("TTS request failed: %s", exc)
            await msg.reply_text("❌ Failed to reach TTS service. Please try again later.")
            return
        except Exception as exc:
            logger.exception("Unexpected error while calling TTS: %s", exc)
            await msg.reply_text("❌ Unexpected error. Please try again later.")
            return

        audio_bytes: Optional[bytes] = None

        # Option 1: audio_url or url field
        audiourl = data.get("audiourl") or data.get("url")
        if audio_url:
            try:
                audiobytes = await fetchbytes(client, audio_url)
            except httpx.HTTPStatusError as exc:
                logger.error("Failed to download audio from url: %s %s", audiourl, exc.response.statuscode)
                audio_bytes = None
            except httpx.RequestError:
                logger.exception("Network error when downloading audio from url: %s", audio_url)
                audio_bytes = None

        # Option 2: base64-encoded audio in payload
        if not audio_bytes and "audio" in data:
            try:
                audio_bytes = base64.b64decode(data["audio"])
            except (TypeError, ValueError) as exc:
                logger.error("Invalid base64 audio from TTS: %s", exc)
                audio_bytes = None

        if not audio_bytes:
            logger.error("No audio returned by TTS service. Response: %s", data)
            await msg.reply_text("❌ TTS did not return audio. Please try again with different text.")
            return

        # Send as voice note (telegram expects bytes-like)
        try:
            await msg.replyvoice(voice=audiobytes)
        except Exception as exc:
            logger.exception("Failed to send voice message: %s", exc)
            await msg.reply_text("❌ Failed to send voice message. Please try again.")


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text("✅ Send me a script and I will narrate it!")


def startpollingin_thread(app):
    """
    Start the Application.run_polling in a daemon thread so the main thread can
    run the web server that binds to PORT (Render requirement for Web Services).
    """
    def _run():
        try:
            app.run_polling()
        except Exception:
            logger.exception("Polling thread exited with error")

    t = threading.Thread(target=_run, name="telegram-polling", daemon=True)
    t.start()
    return t


async def health(request):
    return web.Response(text="ok")


def main() -> None:
    if BOTTOKEN == "YOURTELEGRAMBOTTOKEN" or not BOT_TOKEN:
        logger.error("BOTTOKEN is not set. Set the BOTTOKEN environment variable.")
        return

    # Build telegram application
    app = ApplicationBuilder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.addhandler(MessageHandler(filters.TEXT & ~filters.COMMAND, handlemessage))

    # Start polling in background thread
    startpollingin_thread(app)
    logger.info("Started polling thread for Telegram bot")

    # Start aiohttp web server (bind to PORT required by Render web services)
    port = int(os.environ.get("PORT", "8080"))
    aio_app = web.Application()
    aioapp.router.addget("/", health)
    aioapp.router.addget("/health", health)

    logger.info("Starting web server on port %s", port)
    web.runapp(aioapp, host="0.0.0.0", port=port)


if name == "main":
    main()
`