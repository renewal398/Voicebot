import re
import base64
import requests
from telegram import Update
from telegram.ext import (
    ApplicationBuilder,
    MessageHandler,
    CommandHandler,
    filters,
    ContextTypes
)

BOT_TOKEN = "YOUR_TELEGRAM_BOT_TOKEN"
PUTER_TTS_URL = "https://api.puter.com/v2/ai/tts"


def apply_pauses(text: str):
    """
    Convert [PAUSE Xs] → ... style pauses.
    Example: [PAUSE 1s] → "..."
    """
    def replacer(match):
        amount = float(match.group(1))
        dots = int(amount * 3)  # 0.5s → 1–2 dots
        return "." * max(dots, 3)

    return re.sub(r"\[PAUSE ([0-9.]+)s\]", replacer, text)


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    raw = update.message.text
    await update.message.reply_text("🎙️ Generating voice...")

    processed = apply_pauses(raw)

    payload = {
        "text": processed,
        "engine": "fast",      # neural / fast
        "language": "en-US",
        "voice": "Joanna"
    }

    r = requests.post(PUTER_TTS_URL, json=payload)

    if r.status_code != 200:
        await update.message.reply_text("❌ TTS failed.")
        return

    data = r.json()

    audio_bytes = None

    # ✅ Option 1 — audio_url provided
    audio_url = data.get("audio_url") or data.get("url")
    if audio_url:
        audio_bytes = requests.get(audio_url).content

    # ✅ Option 2 — base64 audio
    if not audio_bytes and "audio" in data:
        audio_bytes = base64.b64decode(data["audio"])

    if not audio_bytes:
        await update.message.reply_text("❌ No audio returned.")
        return

    await update.message.reply_voice(voice=audio_bytes)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "✅ Send me a script and I will narrate it!"
    )


def main():
    app = ApplicationBuilder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    print("BOT RUNNING...")
    app.run_polling()


if __name__ == "__main__":
    main()
