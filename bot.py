import os
import re
import base64
import logging
from typing import Optional

import httpx
from tenacity import retry, stop_after_attempt, wait_exponential, retry_if_exception_type
from telegram import Update
from telegram.ext import (
    ApplicationBuilder,
    MessageHandler,
    CommandHandler,
    filters,
    ContextTypes
)

# Configuration (fallback to environment variables)
BOT_TOKEN = os.getenv("BOT_TOKEN", "YOUR_TELEGRAM_BOT_TOKEN")
PUTER_TTS_URL = os.getenv("PUTER_TTS_URL", "https://api.puter.com/v2/ai/tts")
# Tunables
HTTP_TIMEOUT = 10.0  # seconds
MAX_TEXT_LENGTH = 1200  # avoid extremely long TTS requests
MAX_RETRIES = 3

# Logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


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

    return re.sub(r"\[PAUSE\s*([0-9]*\.?[0-9]+)s\]", replacer, text, flags=re.IGNORECASE)


@retry(
    reraise=True,
    stop=stop_after_attempt(MAX_RETRIES),
    wait=wait_exponential(multiplier=0.5, min=0.5, max=4),
    retry=retry_if_exception_type((httpx.RequestError, httpx.HTTPStatusError))
)
async def post_tts(client: httpx.AsyncClient, payload: dict) -> dict:
    """
    Post to the TTS endpoint with retries for transient errors.
    Raises httpx.HTTPStatusError on non-2xx responses.
    Returns parsed JSON.
    """
    resp = await client.post(PUTER_TTS_URL, json=payload, timeout=HTTP_TIMEOUT)
    resp.raise_for_status()
    return resp.json()


async def fetch_bytes(client: httpx.AsyncClient, url: str) -> bytes:
    resp = await client.get(url, timeout=HTTP_TIMEOUT)
    resp.raise_for_status()
    return resp.content


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    msg = update.message
    if not msg or not msg.text:
        return

    raw = msg.text.strip()
    if not raw:
        await msg.reply_text("Please send some text for narration.")
        return

    if len(raw) > MAX_TEXT_LENGTH:
        await msg.reply_text(f"Message too long (max {MAX_TEXT_LENGTH} characters). Please shorten it.")
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
        audio_url = data.get("audio_url") or data.get("url")
        if audio_url:
            try:
                audio_bytes = await fetch_bytes(client, audio_url)
            except httpx.HTTPStatusError as exc:
                logger.error("Failed to download audio from url: %s %s", audio_url, exc.response.status_code)
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
            await msg.reply_voice(voice=audio_bytes)
        except Exception as exc:
            logger.exception("Failed to send voice message: %s", exc)
            await msg.reply_text("❌ Failed to send voice message. Please try again.")


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text("✅ Send me a script and I will narrate it!")


def main() -> None:
    if BOT_TOKEN == "YOUR_TELEGRAM_BOT_TOKEN" or not BOT_TOKEN:
        logger.error("BOT_TOKEN is not set. Set the BOT_TOKEN environment variable.")
        return

    app = ApplicationBuilder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    logger.info("Bot running...")
    app.run_polling()


if __name__ == "__main__":
    main()