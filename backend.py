import os
import uuid
import sqlite3
import asyncio
import subprocess
import requests
import edge_tts
from dotenv import load_dotenv
from fastapi import FastAPI, UploadFile, File, Form, HTTPException
from fastapi.responses import HTMLResponse, FileResponse
from fastapi.middleware.cors import CORSMiddleware
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, WebAppInfo
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, ContextTypes
import google.generativeai as genai

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
ADMIN_ID = int(os.getenv("ADMIN_ID", "1638781080").strip())
WEBAPP_URL = os.getenv("WEBAPP_URL", "https://example.com").strip()
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "").strip()
ELEVENLABS_API_KEY = os.getenv("ELEVENLABS_API_KEY", "").strip()

if GEMINI_API_KEY:
    genai.configure(api_key=GEMINI_API_KEY)

os.makedirs("outputs", exist_ok=True)
os.makedirs("uploads", exist_ok=True)

app = FastAPI(title="Movie Myanmar Ai")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# --- DATABASE SETUP ---
def init_db():
    conn = sqlite3.connect("movie_myanmar_ai.db")
    c = conn.cursor()
    c.execute("""
        CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY,
            username TEXT,
            points INTEGER DEFAULT 100,
            cloned_voice_id TEXT DEFAULT NULL
        )
    """)
    c.execute("""
        CREATE TABLE IF NOT EXISTS payments (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            plan_name TEXT,
            amount TEXT,
            points INTEGER,
            status TEXT DEFAULT 'pending'
        )
    """)
    conn.commit()
    conn.close()

init_db()

def get_or_create_user(user_id: int, username: str = "User"):
    conn = sqlite3.connect("movie_myanmar_ai.db")
    c = conn.cursor()
    c.execute("SELECT user_id, points, cloned_voice_id FROM users WHERE user_id = ?", (user_id,))
    row = c.fetchone()
    if not row:
        c.execute("INSERT INTO users (user_id, username, points) VALUES (?, ?, 100)", (user_id, username))
        conn.commit()
        row = (user_id, 100, None)
    conn.close()
    return {"user_id": row[0], "points": row[1], "cloned_voice_id": row[2]}

def update_points(user_id: int, delta: int):
    conn = sqlite3.connect("movie_myanmar_ai.db")
    c = conn.cursor()
    c.execute("UPDATE users SET points = MAX(0, points + ?) WHERE user_id = ?", (delta, user_id))
    conn.commit()
    conn.close()

# --- FREE BURMESE MALE VOICES (3 STYLES) ---
FREE_VOICES = {
    "maung_thiha": {
        "name": "မောင်သီဟ (ပုံပြင်ပြောသူ / Recap)",
        "voice": "my-MM-ThihaNeural",
        "pitch": "+0Hz",
        "rate": "+0%"
    },
    "ko_zaw": {
        "name": "ကိုဇော် (သွက်လက် / Commentary)",
        "voice": "my-MM-ThihaNeural",
        "pitch": "+4Hz",
        "rate": "+10%"
    },
    "u_min": {
        "name": "ဦးမင်း (အသံဩဇာ / လေးနက်)",
        "voice": "my-MM-ThihaNeural",
        "pitch": "-5Hz",
        "rate": "-5%"
    }
}

async def generate_free_burmese_tts(text: str, style_key: str, speed_mult: float, out_path: str):
    cfg = FREE_VOICES.get(style_key, FREE_VOICES["maung_thiha"])
    base_rate = int(cfg["rate"].replace("%", ""))
    extra_rate = int((speed_mult - 1.0) * 100)
    total_rate = base_rate + extra_rate
    rate_str = f"{total_rate:+d}%"

    communicate = edge_tts.Communicate(
        text=text,
        voice=cfg["voice"],
        pitch=cfg["pitch"],
        rate=rate_str
    )
    await communicate.save(out_path)

# --- VOICE CLONING (ElevenLabs) ---
def clone_voice_elevenlabs(user_id: int, voice_name: str, file_path: str):
    if not ELEVENLABS_API_KEY:
        raise HTTPException(status_code=400, detail="ELEVENLABS_API_KEY မထည့်ရသေးပါ။ Free အသံ ၃ မျိုးကို အသုံးပြုနိုင်ပါသည်။")
    url = "https://api.elevenlabs.io/v1/voices/add"
    headers = {"xi-api-key": ELEVENLABS_API_KEY}
    data = {"name": f"MMA_{user_id}_{voice_name}"}
    with open(file_path, "rb") as f:
        files = {"files": (os.path.basename(file_path), f, "audio/mpeg")}
        res = requests.post(url, headers=headers, data=data, files=files)
    if res.status_code != 200:
        raise HTTPException(status_code=400, detail=f"Voice Clone မအောင်မြင်ပါ: {res.text}")
    voice_id = res.json().get("voice_id")
    conn = sqlite3.connect("movie_myanmar_ai.db")
    c = conn.cursor()
    c.execute("UPDATE users SET cloned_voice_id = ? WHERE user_id = ?", (voice_id, user_id))
    conn.commit()
    conn.close()
    return voice_id

def generate_cloned_tts(text: str, voice_id: str, out_path: str):
    url = f"https://api.elevenlabs.io/v1/text-to-speech/{voice_id}"
    headers = {
        "xi-api-key": ELEVENLABS_API_KEY,
        "Content-Type": "application/json"
    }
    payload = {
        "text": text,
        "model_id": "eleven_multilingual_v2",
        "voice_settings": {"stability": 0.5, "similarity_boost": 0.8}
    }
    res = requests.post(url, headers=headers, json=payload)
    if res.status_code != 200:
        raise HTTPException(status_code=400, detail="Cloned Voice အသံထုတ်မရပါ။ Free အသံ ၃ မျိုးကို ပြောင်းသုံးပါ။")
    with open(out_path, "wb") as f:
        f.write(res.content)

# --- AUTO TRANSLATE VIDEO AUDIO TO BURMESE SCRIPT ---
def auto_transcribe_and_translate(audio_path: str) -> str:
    if not GEMINI_API_KEY:
        raise HTTPException(status_code=400, detail="GEMINI_API_KEY မထည့်ရသေးပါ။")
    uploaded = genai.upload_file(audio_path)
    model = genai.GenerativeModel("gemini-2.5-flash")
    prompt = (
        "Listen to this video audio and write a natural, engaging Burmese spoken-style movie recap narration script "
        "that summarizes and translates what is happening. Output ONLY the Burmese text without any English or explanations."
    )
    response = model.generate_content([uploaded, prompt])
    return response.text.strip()

# --- TELEGRAM BOT ---
tg_app = Application.builder().token(BOT_TOKEN).build() if BOT_TOKEN else None

async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    get_or_create_user(user.id, user.username or user.first_name)
    keyboard = [
        [InlineKeyboardButton("🎬 Open Movie Myanmar Ai", web_app=WebAppInfo(url=WEBAPP_URL))]
    ]
    await update.message.reply_text(
        f"မင်္ဂလာပါ {user.first_name} 👋\n\n"
        f"**Movie Myanmar Ai** မှ ကြိုဆိုပါတယ်။\n"
        f"• စာသားမှ မြန်မာအသံပြောင်းခြင်း (Free အသံ ၃ မျိုး + Voice Clone)\n"
        f"• နိုင်ငံခြား Video များကို မြန်မာအသံ Recap ပြောင်းခြင်း\n\n"
        f"စတင်အသုံးပြုရန် အောက်ပါခလုတ်ကို နှိပ်ပါ 👇",
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode="Markdown"
    )

async def admin_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if query.from_user.id != ADMIN_ID:
        return

    action, payment_id = query.data.split("_")
    payment_id = int(payment_id)

    conn = sqlite3.connect("movie_myanmar_ai.db")
    c = conn.cursor()
    c.execute("SELECT user_id, plan_name, points, status FROM payments WHERE id = ?", (payment_id,))
    row = c.fetchone()
    if not row or row[3] != "pending":
        await query.edit_message_caption(caption="⚠️ ဤငွေပေးချေမှုကို စစ်ဆေးပြီးသား ဖြစ်ပါသည်။")
        conn.close()
        return

    target_user_id, plan_name, pts, _ = row
    if action == "approve":
        c.execute("UPDATE payments SET status = 'approved' WHERE id = ?", (payment_id,))
        c.execute("UPDATE users SET points = points + ? WHERE user_id = ?", (pts, target_user_id))
        conn.commit()
        await query.edit_message_caption(caption=f"✅ Approved! Order #{payment_id} (+{pts} pts) ထည့်ပေးပြီးပါပြီ။")
        await context.bot.send_message(
            chat_id=target_user_id,
            text=f"🎉 **ငွေပေးချေမှု အတည်ပြုပြီးပါပြီ!**\n"
                 f"Order: `#{payment_id}` ({plan_name})\n"
                 f"သင့်အကောင့်ထဲသို့ **+{pts} Points** ထည့်သွင်းပေးလိုက်ပါပြီ။",
            parse_mode="Markdown"
        )
    else:
        c.execute("UPDATE payments SET status = 'rejected' WHERE id = ?", (payment_id,))
        conn.commit()
        await query.edit_message_caption(caption=f"❌ Rejected! Order #{payment_id} ကို ပယ်ဖျက်လိုက်ပါသည်။")
        await context.bot.send_message(
            chat_id=target_user_id,
            text=f"⚠️ Order `#{payment_id}` ငွေလွှဲပြေစာ အတည်မပြုနိုင်ပါ။ ပြေစာအမှန် ပြန်တင်ပေးပါခင်ဗျာ။",
            parse_mode="Markdown"
        )
    conn.close()

if tg_app:
    tg_app.add_handler(CommandHandler("start", start_cmd))
    tg_app.add_handler(CallbackQueryHandler(admin_callback, pattern="^(approve|reject)_"))

async def start_telegram_bot():
    if not tg_app:
        return
    try:
        await tg_app.initialize()
        await tg_app.start()
        await tg_app.updater.start_polling(drop_pending_updates=True)
        print("Telegram Bot started successfully!")
    except Exception as e:
        print(f"Telegram Bot startup warning: {e}")

@app.on_event("startup")
async def on_startup():
    asyncio.create_task(start_telegram_bot())

# --- API ROUTES ---
@app.get("/", response_class=HTMLResponse)
async def serve_ui():
    with open("index.html", "r", encoding="utf-8") as f:
        return f.read()

@app.get("/api/user/{user_id}")
async def api_user(user_id: int):
    return get_or_create_user(user_id)

@app.post("/api/tts")
async def api_tts(
    user_id: int = Form(...),
    text: str = Form(...),
    voice_type: str = Form(...),
    speed: float = Form(1.0)
):
    user = get_or_create_user(user_id)
    file_id = str(uuid.uuid4())[:8]
    out_path = f"outputs/tts_{file_id}.mp3"

    if voice_type == "cloned":
        cost = max(1, len(text) // 20)
        if user["points"] < cost:
            raise HTTPException(status_code=402, detail="Point မလုံလောက်ပါ။ Free အသံ (၃) မျိုးကို ပြောင်းသုံးနိုင်ပါသည်။")
        if not user["cloned_voice_id"]:
            raise HTTPException(status_code=400, detail="အသံတူပွား (Voice Clone) အရင်ဖန်တီးပါ။")
        generate_cloned_tts(text, user["cloned_voice_id"], out_path)
        update_points(user_id, -cost)
    else:
        await generate_free_burmese_tts(text, voice_type, speed, out_path)

    return {"audio_url": f"/outputs/tts_{file_id}.mp3", "points": get_or_create_user(user_id)["points"]}

@app.post("/api/clone-voice")
async def api_clone_voice(
    user_id: int = Form(...),
    voice_name: str = Form(...),
    sample: UploadFile = File(...)
):
    temp_path = f"uploads/{uuid.uuid4()}_{sample.filename}"
    with open(temp_path, "wb") as f:
        f.write(await sample.read())
    voice_id = clone_voice_elevenlabs(user_id, voice_name, temp_path)
    return {"status": "success", "voice_id": voice_id}

@app.post("/api/auto-script")
async def api_auto_script(video: UploadFile = File(...)):
    job_id = str(uuid.uuid4())[:8]
    in_vid = f"uploads/tmp_{job_id}_{video.filename}"
    in_aud = f"uploads/tmp_{job_id}.mp3"
    with open(in_vid, "wb") as f:
        f.write(await video.read())
    subprocess.run(["ffmpeg", "-y", "-i", in_vid, "-vn", "-acodec", "libmp3lame", in_aud], check=True)
    script = auto_transcribe_and_translate(in_aud)
    return {"script": script}

@app.post("/api/render-recap")
async def api_render_recap(
    user_id: int = Form(...),
    script_text: str = Form(...),
    voice_type: str = Form("maung_thiha"),
    blur_y_percent: int = Form(80),
    aspect_ratio: str = Form("original"),
    video: UploadFile = File(...)
):
    user = get_or_create_user(user_id)
    job_id = str(uuid.uuid4())[:8]
    in_vid = f"uploads/in_{job_id}_{video.filename}"
    tts_audio = f"outputs/recap_audio_{job_id}.mp3"
    out_vid = f"outputs/MovieMyanmarAi_{job_id}.mp4"

    with open(in_vid, "wb") as f:
        f.write(await video.read())

    if voice_type == "cloned" and user["cloned_voice_id"] and user["points"] >= 10:
        generate_cloned_tts(script_text, user["cloned_voice_id"], tts_audio)
        update_points(user_id, -10)
    else:
        await generate_free_burmese_tts(script_text, voice_type if voice_type != "cloned" else "maung_thiha", 1.05, tts_audio)

    vf_filter = f"split[main][blur];[blur]crop=iw:ih*0.18:0:ih*{blur_y_percent/100},boxblur=15:5[blurred];[main][blurred]overlay=0:ih*{blur_y_percent/100}"
    if aspect_ratio == "9:16":
        vf_filter += ",scale=720:1280:force_original_aspect_ratio=decrease,pad=720:1280:(ow-iw)/2:(oh-ih)/2"

    cmd = [
        "ffmpeg", "-y", "-i", in_vid, "-i", tts_audio,
        "-filter_complex", f"[0:v]{vf_filter}[v];[0:a]volume=0.15[bg];[1:a]volume=1.3[fg];[bg][fg]amix=inputs=2:duration=longest[a]",
        "-map", "[v]", "-map", "[a]", "-c:v", "libx264", "-preset", "fast", "-c:a", "aac", out_vid
    ]
    subprocess.run(cmd, check=True)

    if tg_app:
        with open(out_vid, "rb") as vf:
            await tg_app.bot.send_video(
                chat_id=user_id,
                video=vf,
                caption="🎬 **Movie Myanmar Ai** မှ Video Render လုပ်ပြီးပါပြီ!",
                parse_mode="Markdown"
            )

    return {"video_url": f"/{out_vid}"}

@app.post("/api/submit-payment")
async def api_submit_payment(
    user_id: int = Form(...),
    plan_name: str = Form(...),
    amount: str = Form(...),
    points: int = Form(...),
    receipt: UploadFile = File(...)
):
    conn = sqlite3.connect("movie_myanmar_ai.db")
    c = conn.cursor()
    c.execute("INSERT INTO payments (user_id, plan_name, amount, points) VALUES (?, ?, ?, ?)",
              (user_id, plan_name, amount, points))
    payment_id = c.lastrowid
    conn.commit()
    conn.close()

    img_path = f"uploads/receipt_{payment_id}_{receipt.filename}"
    with open(img_path, "wb") as f:
        f.write(await receipt.read())

    if tg_app:
        kb = [[
            InlineKeyboardButton("✅ Approve", callback_data=f"approve_{payment_id}"),
            InlineKeyboardButton("❌ Reject", callback_data=f"reject_{payment_id}")
        ]]
        with open(img_path, "rb") as photo:
            await tg_app.bot.send_photo(
                chat_id=ADMIN_ID,
                photo=photo,
                caption=f"💰 **ငွေလွှဲပြေစာ အသစ် (#{payment_id})**\n"
                        f"👤 User ID: `{user_id}`\n"
                        f"📦 Plan: **{plan_name}** ({amount})\n"
                        f"💎 Points: **+{points} pts**",
                reply_markup=InlineKeyboardMarkup(kb),
                parse_mode="Markdown"
            )

    return {"status": "submitted", "order_id": payment_id}

@app.post("/api/earn-ad")
async def api_earn_ad(user_id: int = Form(...)):
    update_points(user_id, 5)
    return {"points": get_or_create_user(user_id)["points"]}

@app.get("/outputs/{filename}")
async def get_output(filename: str):
    return FileResponse(f"outputs/{filename}")
