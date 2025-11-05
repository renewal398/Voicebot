#!/usr/bin/env python3
import os
import re
import io
import logging
import asyncio
from typing import Optional, List
from gtts import gTTS
import tempfile

from aiohttp import web
from telegram import InputFile, Update
from telegram.ext import (
    ApplicationBuilder,
    MessageHandler,
    CommandHandler,
    filters,
    ContextTypes
)

# Configuration
BOT_TOKEN = os.getenv("BOT_TOKEN", "YOUR_TELEGRAM_BOT_TOKEN")

# Voice options - you can add more here
VOICE_OPTIONS = {
    "us_female": {"lang": "en", "tld": "com", "name": "US Female"},
    "us_male": {"lang": "en", "tld": "com", "name": "US Male"},
    "uk_female": {"lang": "en", "tld": "co.uk", "name": "UK Female"},
    "uk_male": {"lang": "en", "tld": "co.uk", "name": "UK Male"},
    "australian": {"lang": "en", "tld": "com.au", "name": "Australian"},
    "indian": {"lang": "en", "tld": "co.in", "name": "Indian English"},
    "spanish": {"lang": "es", "tld": "es", "name": "Spanish"},
    "french": {"lang": "fr", "tld": "fr", "name": "French"},
    "german": {"lang": "de", "tld": "de", "name": "German"},
}

# Tunables
MAX_TEXT_LENGTH = 5000
CHUNK_DELAY = 1
DEFAULT_VOICE = "us_male"

# Logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


def split_long_text(text: str, max_length: int = MAX_TEXT_LENGTH) -> List[str]:
    """Split long text into chunks that respect sentence boundaries where possible"""
    if len(text) <= max_length:
        return [text]
    
    chunks = []
    current_chunk = ""
    
    sentences = re.split(r'(?<=[.!?])\s+', text)
    
    for sentence in sentences:
        if len(current_chunk) + len(sentence) + 1 > max_length:
            if current_chunk:
                chunks.append(current_chunk.strip())
            current_chunk = sentence
        else:
            if current_chunk:
                current_chunk += " " + sentence
            else:
                current_chunk = sentence
    
    if current_chunk:
        chunks.append(current_chunk.strip())
    
    final_chunks = []
    for chunk in chunks:
        if len(chunk) > max_length:
            paragraphs = chunk.split('\n\n')
            for para in paragraphs:
                if len(para) <= max_length:
                    final_chunks.append(para)
                else:
                    for i in range(0, len(para), max_length):
                        final_chunks.append(para[i:i+max_length])
        else:
            final_chunks.append(chunk)
    
    return final_chunks


def apply_pauses(text: str) -> str:
    """Convert pause tags to natural pauses in speech"""
    def replacer(match: re.Match) -> str:
        try:
            amount = float(match.group(1))
        except (TypeError, ValueError):
            return match.group(0)
        if amount <= 0.3:
            return ","
        else:
            return "." * min(3, int(amount / 0.3))
    
    return re.sub(r"\[PAUSE\s*([0-9]*\.?[0-9]+)s\]", replacer, text, flags=re.IGNORECASE)


async def generate_tts_audio(text: str, voice_key: str = DEFAULT_VOICE) -> Optional[bytes]:
    """Generate TTS audio using gTTS with specified voice"""
    try:
        processed = apply_pauses(text)
        
        voice_config = VOICE_OPTIONS.get(voice_key, VOICE_OPTIONS[DEFAULT_VOICE])
        
        # Create gTTS object with specified voice
        tts = gTTS(
            text=processed,
            lang=voice_config["lang"],
            tld=voice_config["tld"],
            slow=False
        )
        
        audio_buffer = io.BytesIO()
        tts.write_to_fp(audio_buffer)
        audio_buffer.seek(0)
        
        return audio_buffer.getvalue()
        
    except Exception as exc:
        logger.exception("TTS generation failed: %s", exc)
        return None


async def send_voice_message(msg, audio_bytes: bytes, part_num: int = None) -> bool:
    """Send voice message with error handling"""
    try:
        bio = io.BytesIO(audio_bytes)
        bio.seek(0)
        bio.name = "voice.mp3"
        
        caption = None
        if part_num is not None:
            caption = f"Part {part_num}"
            
        await msg.reply_voice(voice=InputFile(bio, filename=bio.name), caption=caption)
        return True
    except Exception as exc:
        logger.exception("Failed to send voice message: %s", exc)
        try:
            bio.seek(0)
            await msg.reply_audio(audio=InputFile(bio, filename="narration.mp3"), caption=caption)
            return True
        except Exception:
            logger.exception("Failed to send audio fallback")
            return False


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    msg = update.message
    if not msg or not msg.text:
        return

    raw = msg.text.strip()
    if not raw:
        await msg.reply_text("Please send some text for narration.")
        return

    # Check if message starts with voice command
    voice_key = DEFAULT_VOICE
    if raw.startswith('/voice '):
        parts = raw.split(' ', 2)
        if len(parts) >= 2:
            requested_voice = parts[1].lower()
            if requested_voice in VOICE_OPTIONS:
                voice_key = requested_voice
                if len(parts) > 2:
                    raw = parts[2]
                else:
                    await msg.reply_text("Please provide text after the voice command.")
                    return
            else:
                await msg.reply_text(
                    f"Unknown voice. Available voices:\n" +
                    "\n".join([f"- {key}: {config['name']}" for key, config in VOICE_OPTIONS.items()])
                )
                return

    # Split long text into manageable chunks
    text_chunks = split_long_text(raw, MAX_TEXT_LENGTH)
    
    if len(text_chunks) > 1:
        await msg.reply_text(
            f"🎙️ Generating {len(text_chunks)} voice messages with {VOICE_OPTIONS[voice_key]['name']} voice..."
        )
    else:
        await msg.reply_text(f"🎙️ Generating voice with {VOICE_OPTIONS[voice_key]['name']}...")

    successful_parts = 0
    
    for i, chunk in enumerate(text_chunks, 1):
        if len(text_chunks) > 1:
            status_msg = await msg.reply_text(f"🔄 Processing part {i}/{len(text_chunks)}...")
        
        audio_bytes = await generate_tts_audio(chunk, voice_key)
        
        if not audio_bytes:
            if len(text_chunks) > 1:
                await msg.reply_text(f"❌ Failed to generate part {i}. Skipping...")
            else:
                await msg.reply_text("❌ Failed to generate voice message. Please try again.")
            continue

        success = await send_voice_message(msg, audio_bytes, i if len(text_chunks) > 1 else None)
        
        if success:
            successful_parts += 1
        
        if len(text_chunks) > 1:
            try:
                await status_msg.delete()
            except Exception:
                pass
        
        if i < len(text_chunks):
            await asyncio.sleep(CHUNK_DELAY)

    if len(text_chunks) > 1:
        if successful_parts == len(text_chunks):
            await msg.reply_text("✅ All voice messages generated successfully!")
        elif successful_parts > 0:
            await msg.reply_text(f"⚠️ Generated {successful_parts}/{len(text_chunks)} voice messages.")
        else:
            await msg.reply_text("❌ Failed to generate any voice messages. Please try again.")


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    voice_list = "\n".join([f"- {key}: {config['name']}" for key, config in VOICE_OPTIONS.items()])
    
    await update.message.reply_text(
        f"✅ Send me a script and I will narrate it!\n\n"
        f"📝 I can handle scripts of any length by splitting them into multiple voice messages.\n\n"
        f"🔊 Available voices (use '/voice <name>' before your text):\n{voice_list}\n\n"
        f"💡 Use [PAUSE 0.5s] to add pauses in your narration.\n\n"
        f"🎯 Default voice: {VOICE_OPTIONS[DEFAULT_VOICE]['name']}"
    )


async def voices(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Command to list all available voices"""
    voice_list = "\n".join([f"- {key}: {config['name']}" for key, config in VOICE_OPTIONS.items()])
    await update.message.reply_text(
        f"🔊 Available voices:\n{voice_list}\n\n"
        f"Usage: /voice <voice_name> <your_text>"
    )


async def aio_health(request):
    return web.Response(text="ok")


async def start_aiohttp_server(port: int) -> web.AppRunner:
    aio_app = web.Application()
    aio_app.router.add_get("/", aio_health)
    aio_app.router.add_get("/health", aio_health)
    runner = web.AppRunner(aio_app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()
    logger.info("aiohttp server started on port %s", port)
    return runner


async def main_async() -> None:
    if BOT_TOKEN == "YOUR_TELEGRAM_BOT_TOKEN" or not BOT_TOKEN:
        logger.error("BOT_TOKEN is not set. Set the BOT_TOKEN environment variable.")
        return

    # Build telegram application
    app = ApplicationBuilder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("voices", voices))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    # Start aiohttp server
    port = int(os.environ.get("PORT", "8080"))
    runner = await start_aiohttp_server(port)

    try:
        await app.initialize()
        await app.start()
        await app.updater.start_polling()
        
        logger.info("Bot started and polling...")
        
        while True:
            await asyncio.sleep(3600)
            
    except asyncio.CancelledError:
        logger.info("Received cancellation signal")
    except Exception:
        logger.exception("Bot encountered an error")
    finally:
        try:
            if app.updater and app.updater.running:
                await app.updater.stop()
        except Exception:
            logger.exception("Error stopping updater")

        try:
            await app.stop()
        except Exception:
            logger.exception("Error stopping telegram app")

        try:
            await app.shutdown()
        except Exception:
            logger.exception("Error shutting down telegram app")

        try:
            await runner.cleanup()
        except Exception:
            logger.exception("Error while shutting down aiohttp runner")


def main() -> None:
    asyncio.run(main_async())


if __name__ == "__main__":
    main()