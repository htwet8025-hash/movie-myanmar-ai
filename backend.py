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
WEBAPP_URL = os.getenv("WEBAPP_URL", "https://movie-myanmar-ai-2.onrender.com").strip()
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

# --- EMBEDDED MINI APP HTML ---
HTML_UI = """<!DOCTYPE html>
<html lang="my">
<head>
  <meta charset="UTF-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1.0" />
  <title>Movie Myanmar Ai</title>
  <script src="https://telegram.org/js/telegram-web-app.js"></script>
  <script src="https://cdn.tailwindcss.com"></script>
  <style>
    body { background-color: #0b0e14; color: #f1f5f9; font-family: sans-serif; }
    .card { background-color: #151a23; border: 1px solid #242c3a; }
    .tab-active { color: #06b6d4; border-top: 2px solid #06b6d4; }
  </style>
</head>
<body class="pb-20">

  <div class="sticky top-0 z-20 bg-[#0b0e14]/95 backdrop-blur border-b border-gray-800 px-4 py-3 flex justify-between items-center">
    <div class="flex items-center gap-2">
      <div class="w-8 h-8 rounded-lg bg-gradient-to-tr from-cyan-500 to-blue-600 flex items-center justify-center font-bold text-white">M</div>
      <div>
        <h1 class="font-bold text-base text-cyan-400 leading-tight">Movie Myanmar Ai</h1>
        <p class="text-[11px] text-gray-400">AI Voice & Video Recap Studio</p>
      </div>
    </div>
    <div class="bg-cyan-950/60 border border-cyan-500/30 px-3 py-1 rounded-full text-xs font-semibold text-cyan-300">
      💎 <span id="userPoints">100</span> pts
    </div>
  </div>

  <!-- TAB 1: STUDIO -->
  <div id="tab-studio" class="p-4 space-y-4">
    <div class="grid grid-cols-2 gap-2 bg-[#151a23] p-1 rounded-xl border border-gray-800">
      <button onclick="switchStudioMode('tts')" id="btn-mode-tts" class="py-2 rounded-lg text-xs font-bold bg-cyan-600 text-white">🎙️ စာ ➔ အသံ (TTS)</button>
      <button onclick="switchStudioMode('video')" id="btn-mode-video" class="py-2 rounded-lg text-xs font-bold text-gray-400">🎬 Video ➔ မြန်မာအသံ</button>
    </div>

    <div class="card p-4 rounded-2xl space-y-3">
      <div class="flex justify-between items-center">
        <label class="text-xs font-bold text-gray-300">အသံရွေးချယ်ပါ (Voice Selection)</label>
        <button onclick="document.getElementById('cloneModal').classList.remove('hidden')" class="text-[11px] bg-purple-600/20 text-purple-300 border border-purple-500/40 px-2.5 py-1 rounded-lg">
          + အသံတူပွား (Clone)
        </button>
      </div>
      <select id="voiceSelect" class="w-full bg-[#0b0e14] border border-gray-700 rounded-xl p-2.5 text-sm text-white">
        <option value="maung_thiha">🆓 မောင်သီဟ - ပုံပြင်ပြောသူ (Free / Recap)</option>
        <option value="ko_zaw">🆓 ကိုဇော် - သွက်လက်တင်ဆက်သူ (Free / Fast)</option>
        <option value="u_min">🆓 ဦးမင်း - အသံဩဇာလေးနက် (Free / Deep)</option>
        <option value="cloned">⭐ ကျွန်ုပ်၏ အသံတူပွား (My Cloned Voice - Uses Points)</option>
      </select>
      <p class="text-[11px] text-emerald-400">💡 Point ကုန်သွားပါက Free မြန်မာယောက်ျားလေးအသံ (၃) မျိုးကို အကန့်အသတ်မရှိ ဆက်သုံးနိုင်ပါသည်။</p>
    </div>

    <!-- MODE A: TEXT TO SPEECH -->
    <div id="mode-tts" class="card p-4 rounded-2xl space-y-3">
      <label class="text-xs font-bold text-gray-300">မြန်မာစာသား ရိုက်ထည့်ပါ</label>
      <textarea id="ttsText" rows="5" placeholder="ဒီနေ့ ပြောပြပေးမယ့် ဇာတ်ကားလေးကတော့..." class="w-full bg-[#0b0e14] border border-gray-700 rounded-xl p-3 text-sm text-white focus:border-cyan-500 outline-none"></textarea>
      
      <div class="flex items-center justify-between text-xs text-gray-400">
        <span>အသံအမြန်နှုန်း: <b id="speedVal" class="text-cyan-400">1.0x</b></span>
        <input type="range" id="speedSlider" min="0.8" max="1.4" step="0.1" value="1.0" oninput="document.getElementById('speedVal').innerText=this.value+'x'" class="w-36 accent-cyan-500">
      </div>

      <button onclick="generateTTS()" id="ttsBtn" class="w-full py-3 rounded-xl bg-gradient-to-r from-cyan-500 to-blue-600 font-bold text-sm text-white shadow-lg">
        🔊 အသံဖိုင် ထုတ်မည်
      </button>

      <div id="ttsResult" class="hidden pt-2">
        <audio id="audioPlayer" controls class="w-full"></audio>
      </div>
    </div>

    <!-- MODE B: VIDEO RECAP MAKER -->
    <div id="mode-video" class="hidden card p-4 rounded-2xl space-y-3">
      <label class="text-xs font-bold text-gray-300">၁။ နိုင်ငံခြား Video ဖိုင် ရွေးပါ (MP4)</label>
      <input type="file" id="videoFile" accept="video/*" class="w-full text-xs text-gray-300 bg-[#0b0e14] border border-gray-700 rounded-xl p-2" />

      <button onclick="autoGenerateScript()" id="autoScriptBtn" class="w-full py-2 rounded-xl bg-indigo-600/30 border border-indigo-500/50 text-indigo-300 font-bold text-xs">
        ✨ AI ဖြင့် Video ထဲကအသံကို မြန်မာစာသား အလိုအလျောက် ပြောင်းမည်
      </button>

      <label class="text-xs font-bold text-gray-300">၂။ နောက်ခံပြောမည့် မြန်မာစာသား (ပြန်ပြင်နိုင်သည်)</label>
      <textarea id="recapScript" rows="4" placeholder="ဇာတ်လမ်းအစမှာ မင်းသားက..." class="w-full bg-[#0b0e14] border border-gray-700 rounded-xl p-3 text-sm text-white"></textarea>

      <div class="grid grid-cols-2 gap-2">
        <div>
          <label class="text-[11px] text-gray-400">မူရင်းစာတန်း Blur ဖုံးမည့်နေရာ (%)</label>
          <input type="number" id="blurPos" value="80" min="0" max="90" class="w-full bg-[#0b0e14] border border-gray-700 rounded-lg p-2 text-xs text-white mt-1" />
        </div>
        <div>
          <label class="text-[11px] text-gray-400">Video အချိုး (Aspect Ratio)</label>
          <select id="aspectRatio" class="w-full bg-[#0b0e14] border border-gray-700 rounded-lg p-2 text-xs text-white mt-1">
            <option value="original">မူရင်းအတိုင်း (Original)</option>
            <option value="9:16">TikTok / Shorts (9:16)</option>
          </select>
        </div>
      </div>

      <button onclick="renderVideoRecap()" id="renderBtn" class="w-full py-3 rounded-xl bg-gradient-to-r from-emerald-500 to-cyan-600 font-bold text-sm text-white shadow-lg">
        🎬 Video Render လုပ်ပြီး Bot ထဲပို့မည်
      </button>
    </div>
  </div>

  <!-- TAB 2: PLANS & PAYMENT -->
  <div id="tab-plans" class="hidden p-4 space-y-4">
    <div class="card p-4 rounded-2xl border-cyan-500/40 space-y-2">
      <h2 class="font-bold text-sm text-cyan-400">💳 ငွေလွှဲလက်ခံမည့် အကောင့်များ</h2>
      <p class="text-xs text-gray-300">အမည်: <b class="text-white">Kaung San Phyo</b></p>
      <div class="flex items-center justify-between bg-[#0b0e14] p-2.5 rounded-xl border border-gray-800">
        <div>
          <p class="text-xs text-gray-400">KBZPay / WavePay / UABPay</p>
          <p class="font-mono font-bold text-base text-amber-400">09409700002</p>
        </div>
        <button onclick="copyPhone()" class="bg-cyan-600 text-white text-xs px-3 py-1.5 rounded-lg font-bold">Copy</button>
      </div>
    </div>

    <div class="space-y-2.5">
      <div onclick="selectPlan('Starter Plan', '15,000 MMK', 1500)" class="card p-4 rounded-2xl flex justify-between items-center cursor-pointer hover:border-cyan-500">
        <div>
          <h3 class="font-bold text-sm text-white">Starter Plan</h3>
          <p class="text-xs text-gray-400">1,500 Points (Voice Clone & Pro TTS)</p>
        </div>
        <span class="bg-cyan-500/20 text-cyan-300 font-bold text-xs px-3 py-1.5 rounded-xl">15,000 MMK</span>
      </div>

      <div onclick="selectPlan('Pro Plan', '40,000 MMK', 5000)" class="card p-4 rounded-2xl flex justify-between items-center cursor-pointer border-amber-500/40 hover:border-amber-400">
        <div>
          <h3 class="font-bold text-sm text-amber-400">Pro Plan 🔥</h3>
          <p class="text-xs text-gray-400">5,000 Points + Priority Render</p>
        </div>
        <span class="bg-amber-500/20 text-amber-300 font-bold text-xs px-3 py-1.5 rounded-xl">40,000 MMK</span>
      </div>

      <div onclick="selectPlan('Business Plan', '88,000 MMK', 12500)" class="card p-4 rounded-2xl flex justify-between items-center cursor-pointer hover:border-cyan-500">
        <div>
          <h3 class="font-bold text-sm text-purple-400">Business Plan</h3>
          <p class="text-xs text-gray-400">12,500 Points (အများဆုံးသက်သာ)</p>
        </div>
        <span class="bg-purple-500/20 text-purple-300 font-bold text-xs px-3 py-1.5 rounded-xl">88,000 MMK</span>
      </div>
    </div>

    <div id="paymentBox" class="hidden card p-4 rounded-2xl space-y-3 border-emerald-500/50">
      <h3 class="text-xs font-bold text-emerald-400">ရွေးချယ်ထားသော Plan: <span id="selectedPlanLabel"></span></h3>
      <label class="block text-xs text-gray-300">ငွေလွှဲပြေစာ (Screenshot) တင်ပေးပါ:</label>
      <input type="file" id="receiptFile" accept="image/*" class="w-full text-xs bg-[#0b0e14] p-2 rounded-xl border border-gray-700" />
      <button onclick="submitPayment()" class="w-full py-2.5 rounded-xl bg-emerald-600 font-bold text-xs text-white">✅ ငွေလွှဲပြေစာ ပေးပို့မည်</button>
    </div>
  </div>

  <!-- TAB 3: EARN FREE POINTS -->
  <div id="tab-earn" class="hidden p-4 space-y-4">
    <div class="card p-5 rounded-2xl text-center space-y-3">
      <div class="text-3xl">🎁</div>
      <h2 class="font-bold text-base text-white">အခမဲ့ Point စုဆောင်းပါ</h2>
      <p class="text-xs text-gray-400">ကြော်ငြာကြည့်ရှုပြီး Point များ အခမဲ့ ရယူနိုင်သလို Free မြန်မာအသံ (၃) မျိုးကိုလည်း အချိန်မရွေး အခမဲ့ သုံးနိုင်ပါသည်။</p>
      <button onclick="earnFreePoints()" class="w-full py-3 rounded-xl bg-gradient-to-r from-amber-500 to-orange-600 font-bold text-sm text-white">
        📺 ကြော်ငြာကြည့်ပြီး +5 Points ယူမည်
      </button>
    </div>
  </div>

  <!-- TAB 4: PROFILE -->
  <div id="tab-profile" class="hidden p-4 space-y-4">
    <div class="card p-4 rounded-2xl space-y-2">
      <h2 class="font-bold text-sm text-cyan-400">👤 အကောင့်အချက်အလက်</h2>
      <p class="text-xs text-gray-300">Telegram ID: <span id="profId" class="font-mono text-white"></span></p>
      <p class="text-xs text-gray-300">လက်ကျန် Point: <span id="profPts" class="font-bold text-amber-400"></span></p>
      <p class="text-xs text-gray-300">Tool အမည်: <span class="text-white font-semibold">Movie Myanmar Ai</span></p>
    </div>
  </div>

  <!-- VOICE CLONE MODAL -->
  <div id="cloneModal" class="hidden fixed inset-0 bg-black/80 z-50 flex items-center justify-center p-4">
    <div class="card w-full max-w-sm p-5 rounded-2xl space-y-3">
      <h3 class="font-bold text-sm text-purple-400">🎙️ အသံတူပွား ဖန်တီးရန် (Voice Cloning)</h3>
      <p class="text-xs text-gray-400">ဆူညံသံမပါသော ၅ စက္ကန့်မှ ၁၅ စက္ကန့်အတွင်း အသံဖိုင် (MP3/WAV) တင်ပေးပါ။</p>
      <input type="text" id="cloneName" placeholder="အသံအမည် (ဥပမာ- My Voice)" class="w-full bg-[#0b0e14] border border-gray-700 rounded-xl p-2.5 text-xs text-white" />
      <input type="file" id="cloneSample" accept="audio/*" class="w-full text-xs bg-[#0b0e14] border border-gray-700 rounded-xl p-2" />
      <div class="flex gap-2 pt-2">
        <button onclick="document.getElementById('cloneModal').classList.add('hidden')" class="flex-1 py-2 rounded-xl bg-gray-800 text-xs">ပိတ်မည်</button>
        <button onclick="uploadVoiceClone()" id="cloneBtn" class="flex-1 py-2 rounded-xl bg-purple-600 font-bold text-xs text-white">Clone လုပ်မည်</button>
      </div>
    </div>
  </div>

  <!-- BOTTOM NAVIGATION -->
  <div class="fixed bottom-0 inset-x-0 bg-[#151a23] border-t border-gray-800 grid grid-cols-4 text-center py-2 text-[11px] font-semibold">
    <button onclick="showTab('studio')" id="nav-studio" class="tab-active py-1">🎬 Studio</button>
    <button onclick="showTab('plans')" id="nav-plans" class="text-gray-400 py-1">💎 Plans</button>
    <button onclick="showTab('earn')" id="nav-earn" class="text-gray-400 py-1">🎁 Earn</button>
    <button onclick="showTab('profile')" id="nav-profile" class="text-gray-400 py-1">👤 Profile</button>
  </div>

  <script>
    const tg = window.Telegram.WebApp;
    tg.expand();
    const userId = tg.initDataUnsafe?.user?.id || 1638781080;
    document.getElementById("profId").innerText = userId;

    let selectedPlan = {};

    async function loadUser() {
      const res = await fetch(`/api/user/${userId}`);
      const data = await res.json();
      document.getElementById("userPoints").innerText = data.points;
      document.getElementById("profPts").innerText = data.points + " pts";
    }
    loadUser();

    function showTab(name) {
      ["studio", "plans", "earn", "profile"].forEach(t => {
        document.getElementById(`tab-${t}`).classList.add("hidden");
        document.getElementById(`nav-${t}`).classList.remove("tab-active");
        document.getElementById(`nav-${t}`).classList.add("text-gray-400");
      });
      document.getElementById(`tab-${name}`).classList.remove("hidden");
      document.getElementById(`nav-${name}`).classList.add("tab-active");
      document.getElementById(`nav-${name}`).classList.remove("text-gray-400");
    }

    function switchStudioMode(mode) {
      if (mode === "tts") {
        document.getElementById("mode-tts").classList.remove("hidden");
        document.getElementById("mode-video").classList.add("hidden");
        document.getElementById("btn-mode-tts").className = "py-2 rounded-lg text-xs font-bold bg-cyan-600 text-white";
        document.getElementById("btn-mode-video").className = "py-2 rounded-lg text-xs font-bold text-gray-400";
      } else {
        document.getElementById("mode-tts").classList.add("hidden");
        document.getElementById("mode-video").classList.remove("hidden");
        document.getElementById("btn-mode-video").className = "py-2 rounded-lg text-xs font-bold bg-cyan-600 text-white";
        document.getElementById("btn-mode-tts").className = "py-2 rounded-lg text-xs font-bold text-gray-400";
      }
    }

    async function generateTTS() {
      const text = document.getElementById("ttsText").value.trim();
      if (!text) return alert("စာသားအရင် ရိုက်ထည့်ပါ။");
      const btn = document.getElementById("ttsBtn");
      btn.innerText = "⏳ အသံထုတ်နေပါသည်...";
      btn.disabled = true;

      const fd = new FormData();
      fd.append("user_id", userId);
      fd.append("text", text);
      fd.append("voice_type", document.getElementById("voiceSelect").value);
      fd.append("speed", document.getElementById("speedSlider").value);

      const res = await fetch("/api/tts", { method: "POST", body: fd });
      const data = await res.json();
      btn.innerText = "🔊 အသံဖိုင် ထုတ်မည်";
      btn.disabled = false;

      if (!res.ok) return alert(data.detail || "Error");
      document.getElementById("ttsResult").classList.remove("hidden");
      document.getElementById("audioPlayer").src = data.audio_url;
      document.getElementById("audioPlayer").play();
      loadUser();
    }

    async function uploadVoiceClone() {
      const name = document.getElementById("cloneName").value.trim();
      const file = document.getElementById("cloneSample").files[0];
      if (!name || !file) return alert("အမည်နှင့် အသံဖိုင် ထည့်ပါ။");

      const btn = document.getElementById("cloneBtn");
      btn.innerText = "⏳ လုပ်ဆောင်နေသည်...";
      const fd = new FormData();
      fd.append("user_id", userId);
      fd.append("voice_name", name);
      fd.append("sample", file);

      const res = await fetch("/api/clone-voice", { method: "POST", body: fd });
      const data = await res.json();
      btn.innerText = "Clone လုပ်မည်";
      if (!res.ok) return alert(data.detail);
      alert("✅ အသံတူပွား ဖန်တီးပြီးပါပြီ!");
      document.getElementById("cloneModal").classList.add("hidden");
      document.getElementById("voiceSelect").value = "cloned";
    }

    async function autoGenerateScript() {
      const file = document.getElementById("videoFile").files[0];
      if (!file) return alert("Video ဖိုင် အရင်ရွေးပါ။");
      const btn = document.getElementById("autoScriptBtn");
      btn.innerText = "⏳ AI ဖြင့် နားထောင်ပြီး မြန်မာလို ဘာသာပြန်နေပါသည်...";
      btn.disabled = true;

      const fd = new FormData();
      fd.append("video", file);
      const res = await fetch("/api/auto-script", { method: "POST", body: fd });
      const data = await res.json();
      btn.innerText = "✨ AI ဖြင့် Video ထဲကအသံကို မြန်မာစာသား အလိုအလျောက် ပြောင်းမည်";
      btn.disabled = false;

      if (!res.ok) return alert(data.detail || "Error");
      document.getElementById("recapScript").value = data.script;
    }

    async function renderVideoRecap() {
      const file = document.getElementById("videoFile").files[0];
      const script = document.getElementById("recapScript").value.trim();
      if (!file || !script) return alert("Video ဖိုင်နှင့် မြန်မာစာသား ထည့်ပါ။");

      const btn = document.getElementById("renderBtn");
      btn.innerText = "⏳ Render လုပ်နေပါသည် (ခဏစောင့်ပါ)...";
      btn.disabled = true;

      const fd = new FormData();
      fd.append("user_id", userId);
      fd.append("script_text", script);
      fd.append("voice_type", document.getElementById("voiceSelect").value);
      fd.append("blur_y_percent", document.getElementById("blurPos").value);
      fd.append("aspect_ratio", document.getElementById("aspectRatio").value);
      fd.append("video", file);

      const res = await fetch("/api/render-recap", { method: "POST", body: fd });
      btn.innerText = "🎬 Video Render လုပ်ပြီး Bot ထဲပို့မည်";
      btn.disabled = false;

      if (res.ok) {
        alert("✅ Video Render ပြီးပါပြီ! သင့် Telegram Bot Chat ထဲသို့ ပို့ပေးလိုက်ပါပြီ။");
        loadUser();
      } else {
        alert("Render မအောင်မြင်ပါ။");
      }
    }

    function copyPhone() {
      navigator.clipboard.writeText("09409700002");
      alert("ဖုန်းနံပါတ် 09409700002 (Kaung San Phyo) ကို Copy ကူးပြီးပါပြီ။");
    }

    function selectPlan(name, amount, pts) {
      selectedPlan = { name, amount, pts };
      document.getElementById("selectedPlanLabel").innerText = `${name} (${amount} - ${pts} pts)`;
      document.getElementById("paymentBox").classList.remove("hidden");
    }

    async function submitPayment() {
      const file = document.getElementById("receiptFile").files[0];
      if (!file) return alert("ငွေလွှဲပြေစာ ပုံရွေးပါ။");

      const fd = new FormData();
      fd.append("user_id", userId);
      fd.append("plan_name", selectedPlan.name);
      fd.append("amount", selectedPlan.amount);
      fd.append("points", selectedPlan.pts);
      fd.append("receipt", file);

      const res = await fetch("/api/submit-payment", { method: "POST", body: fd });
      if (res.ok) {
        alert("✅ ငွေလွှဲပြေစာ ပေးပို့ပြီးပါပြီ! Admin စစ်ဆေးပြီးပါက Point ထည့်ပေးပါမည်။");
        document.getElementById("paymentBox").classList.add("hidden");
      }
    }

    async function earnFreePoints() {
      const fd = new FormData();
      fd.append("user_id", userId);
      const res = await fetch("/api/earn-ad", { method: "POST", body: fd });
      if (res.ok) {
        alert("🎉 +5 Points ရရှိပါပြီ!");
        loadUser();
      }
    }
  </script>
</body>
</html>"""

# --- API ROUTES ---
@app.get("/", response_class=HTMLResponse)
async def serve_ui():
    return HTMLResponse(content=HTML_UI)

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
