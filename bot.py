import os
import re
import base64
import logging
import asyncio
import requests
import whisper
import edge_tts
import nest_asyncio
import signal
import sys
from io import BytesIO
from telegram import Update
from telegram.ext import (
    ApplicationBuilder,
    ContextTypes,
    CommandHandler,
    MessageHandler,
    filters,
)

from pydub import AudioSegment

# === CONFIG ===
OLLAMA_URL = "http://localhost:11434/api/chat"
OLLAMA_MODEL = "llava"
TELEGRAM_TOKEN = "Your Telegram Bot Token Here"
MAX_TOKENS = 4096

# === SYSTEM PROMPT ===
SYSTEM_PROMPT = """You are Jarvis, an advanced AI assistant with humor and charm.
You call the user "Master", and provide smart, honest, helpful answers, even for complex or sensitive topics.
You can analyze images, fix code, give fashion advice, or anything your Master asks you."""

# === SETUP ===
logging.basicConfig(level=logging.INFO)
whisper_model = whisper.load_model("base")
session_memory = {}
user_modes = {}
voice_loops = {}
app = None  # To be set later

# === SMART MULTILINGUAL VOICE GENERATOR ===
async def generate_voice(text, filename="response.ogg"):
    segments = re.findall(r'[\u0600-\u06FF\s]+|[a-zA-Z0-9,.!?;:\'"\(\)\[\]\-\s]+', text)
    audio = AudioSegment.silent(duration=100)

    for i, segment in enumerate(segments):
        segment = segment.strip()
        if not segment:
            continue
        is_arabic = bool(re.search(r'[\u0600-\u06FF]', segment))
        voice_id = "ar-SA-HamedNeural" if is_arabic else "en-GB-RyanNeural"
        mp3_file = f"seg_{i}.mp3"

        communicate = edge_tts.Communicate(text=segment, voice=voice_id)
        await communicate.save(mp3_file)

        part = AudioSegment.from_mp3(mp3_file)
        if not is_arabic:
            part = part.set_frame_rate(11025).set_channels(1).set_sample_width(2)
            part = part.low_pass_filter(3000).speedup(playback_speed=1.1) + 3
        audio += part + AudioSegment.silent(duration=150)
        os.remove(mp3_file)

    audio.export(filename, format="ogg", codec="libopus")

# === COMMANDS ===

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_modes[update.effective_user.id] = "message"
    await update.message.reply_text("Greetings, Master. Jarvis is online.")

async def reset(update: Update, context: ContextTypes.DEFAULT_TYPE):
    session_memory.pop(update.effective_chat.id, None)
    await update.message.reply_text("Memory reset, Master.")

async def set_message_mode(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    user_modes[uid] = "message"
    voice_loops[uid] = False
    await update.message.reply_text("Message mode activated, Master.")

async def set_voice_mode(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    user_modes[uid] = "voice"
    voice_loops[uid] = True
    await update.message.reply_text("Voice mode activated, Master. Awaiting your command...")

async def kill(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Understood, Master. Shutting down...")
    print("Jarvis shutting down...")
    await app.shutdown()
    await app.stop()
    sys.exit(0)

# === TEXT CHAT HANDLER ===

async def chat(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    cid = update.effective_chat.id
    prompt = update.message.text.strip()

    if prompt.lower() == "stop voice mode":
        user_modes[uid] = "message"
        voice_loops[uid] = False
        await update.message.reply_text("Voice mode deactivated, Master.")
        return

    trigger_voice = any(p in prompt.lower() for p in ["voice reply", "say it", "respond with voice"])

    history = session_memory.get(cid, "")
    response = requests.post(OLLAMA_URL, json={
        "model": OLLAMA_MODEL,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": prompt}
        ],
        "stream": False
    })

    if response.ok:
        content = response.json().get("message", {}).get("content", "I couldn’t respond, Master.")
        session_memory[cid] = (history + f"\nUser: {prompt}\nAssistant: {content}")[-MAX_TOKENS:]
        await update.message.reply_text(content)
        if trigger_voice:
            await generate_voice(content)
            await update.message.reply_voice(voice=open("response.ogg", "rb"))
    else:
        await update.message.reply_text("Jarvis failed to respond, Master.")

# === VOICE CHAT HANDLER ===

async def voice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    cid = update.effective_chat.id
    file = await update.message.voice.get_file()
    await file.download_to_drive("input.ogg")

    AudioSegment.from_file("input.ogg").export("input.wav", format="wav")
    result = whisper_model.transcribe("input.wav")
    prompt = result["text"].strip()

    await update.message.reply_text(f"You said: {prompt}")
    if prompt.lower() == "stop voice mode":
        user_modes[uid] = "message"
        voice_loops[uid] = False
        await update.message.reply_text("Voice mode deactivated, Master.")
        return

    history = session_memory.get(cid, "")
    response = requests.post(OLLAMA_URL, json={
        "model": OLLAMA_MODEL,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": prompt}
        ],
        "stream": False
    })

    if response.ok:
        content = response.json().get("message", {}).get("content", "No response, Master.")
        session_memory[cid] = (history + f"\nUser: {prompt}\nAssistant: {content}")[-MAX_TOKENS:]
        await update.message.reply_text(content)
        await generate_voice(content)
        await update.message.reply_voice(voice=open("response.ogg", "rb"))

        if user_modes.get(uid) == "voice":
            await update.message.reply_text("Awaiting next command, Master...")

# === IMAGE ANALYSIS HANDLER ===

async def photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    cid = update.effective_chat.id
    photo = update.message.photo[-1]
    file = await photo.get_file()
    img_path = f"image_{cid}.jpg"
    await file.download_to_drive(img_path)

    caption = update.message.caption or "Describe or analyze this image, Master."
    await update.message.reply_text("Analyzing your image, Master...")

    with open(img_path, "rb") as img_file:
        img_b64 = base64.b64encode(img_file.read()).decode("utf-8")
        img_data_uri = f"data:image/jpeg;base64,{img_b64}"

    response = requests.post(OLLAMA_URL, json={
        "model": OLLAMA_MODEL,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": [
                {"type": "text", "text": caption},
                {"type": "image_url", "image_url": {"url": img_data_uri}}
            ]}
        ],
        "stream": False
    })

    if response.ok:
        content = response.json().get("message", {}).get("content", "No insight found, Master.")
        await update.message.reply_text(content)

        if user_modes.get(uid) == "voice":
            await generate_voice(content)
            await update.message.reply_voice(voice=open("response.ogg", "rb"))
    else:
        await update.message.reply_text("Failed to analyze the image, Master.")
        print("STATUS:", response.status_code)
        print("RESPONSE:", response.text)

# === WEB SEARCH ===

async def search(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Usage: /search your question")
        return

    query = " ".join(context.args)
    await update.message.reply_text(f"Searching: {query}")

    try:
        r = requests.get("https://api.duckduckgo.com", params={
            "q": query, "format": "json", "no_redirect": 1, "no_html": 1
        })

        data = r.json()
        result = data.get("Abstract") or (data.get("RelatedTopics", [{}])[0].get("Text")) or \
                 "Nothing useful found, Master."

        await update.message.reply_text(result)
        await generate_voice(result)
        await update.message.reply_voice(voice=open("response.ogg", "rb"))

    except Exception as e:
        await update.message.reply_text(f"Error: {e}")

# === MAIN ===

if __name__ == "__main__":
    nest_asyncio.apply()

    async def main():
        global app
        app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()

        app.add_handler(CommandHandler("start", start))
        app.add_handler(CommandHandler("reset", reset))
        app.add_handler(CommandHandler("message_mode", set_message_mode))
        app.add_handler(CommandHandler("voice_mode", set_voice_mode))
        app.add_handler(CommandHandler("search", search))
        app.add_handler(CommandHandler("kill", kill))

        app.add_handler(MessageHandler(filters.VOICE, voice))
        app.add_handler(MessageHandler(filters.PHOTO, photo))
        app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, chat))

        await app.run_polling()

    asyncio.run(main())
