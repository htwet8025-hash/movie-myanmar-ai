import os
import uuid
import base64
import sqlite3
import asyncio
import subprocess
import traceback
import requests
import edge_tts
from dotenv import load_dotenv
from fastapi import FastAPI, UploadFile, File, Form, HTTPException
from fastapi.responses import HTMLResponse, FileResponse
from fastapi.middleware.cors import CORSMiddleware
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, WebAppInfo
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, ContextTypes

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
ADMIN_ID = int(os.getenv("ADMIN_ID", "1638781080").strip())
WEBAPP_URL = os.getenv("WEBAPP_URL", "https://movie-myanmar-ai-2.onrender.com").strip()
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "").strip()
ELEVENLABS_API_KEY = os.getenv("ELEVENLABS_API_KEY", "").strip()

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

# --- MEDIA HELPERS ---
def get_media_duration(file_path: str) -> float:
    try:
        cmd = [
            "ffprobe", "-v", "error", "-show_entries", "format=duration",
            "-of", "default=noprint_wrappers=1:nokey=1", file_path
        ]
        res = subprocess.run(cmd, capture_output=True, text=True)
        return float(res.stdout.strip())
    except Exception:
        return 0.0

def has_audio_stream(file_path: str) -> bool:
    try:
        cmd = [
            "ffprobe", "-v", "error", "-select_streams", "a",
            "-show_entries", "stream=codec_type", "-of", "csv=p=0", file_path
        ]
        res = subprocess.run(cmd, capture_output=True, text=True)
        return "audio" in res.stdout
    except Exception:
        return False

# --- FREE BURMESE MALE VOICES (3 STYLES) ---
FREE_VOICES = {
    "maung_thiha": {
        "voice": "my-MM-ThihaNeural",
        "pitch": "+0Hz",
        "rate": "+0%",
        "sample": "မင်္ဂလာပါခင်ဗျာ။ ကျွန်တော်ကတော့ မောင်သီဟ ဖြစ်ပါတယ်။ ဇာတ်လမ်းအကျဉ်းချုပ်တွေကို နားထောင်လို့ကောင်းအောင် ပြောပြပေးပါမယ်။"
    },
    "ko_zaw": {
        "voice": "my-MM-ThihaNeural",
        "pitch": "+4Hz",
        "rate": "+10%",
        "sample": "ဟုတ်ကဲ့ အခုဆိုရင် ပွဲချိန်စတင်နေပါပြီခင်ဗျာ။ ကျွန်တော် ကိုဇော် ကနေ သွက်သွက်လက်လက် တင်ဆက်ပေးသွားမှာပါ။"
    },
    "u_min": {
        "voice": "my-MM-ThihaNeural",
        "pitch": "-5Hz",
        "rate": "-5%",
        "sample": "လျှို့ဝှက်ဆန်းကြယ်တဲ့ ဒီဇာတ်လမ်းရဲ့ နောက်ကွယ်မှာ ဘာတွေရှိနေမလဲဆိုတာ ဦးမင်းနဲ့အတူ ကြည့်ရှုလိုက်ရအောင်။"
    },
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

# --- VOICE CLONING (MP3/WAV + MP4 VIDEO) ---
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
    headers = {"xi-api-key": ELEVENLABS_API_KEY, "Content-Type": "application/json"}
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

# --- FAST AUTO TRANSLATE (SAFE & SUPPORTS ALL GEMINI MODELS) ---
def call_gemini_audio(audio_path: str, duration_sec: float) -> str:
    if not GEMINI_API_KEY:
        raise HTTPException(status_code=400, detail="GEMINI_API_KEY မထည့်ရသေးပါ။")

    with open(audio_path, "rb") as f:
        audio_b64 = base64.b64encode(f.read()).decode("utf-8")

    target_chars = max(40, int(duration_sec * 16)) if duration_sec > 0 else 250
    prompt = (
        f"Listen to this audio (duration: {duration_sec:.1f} seconds). "
        f"Translate and narrate what is happening into natural spoken Burmese movie-recap style. "
        f"Keep the Burmese script length around {target_chars} characters so when read aloud it finishes in about {duration_sec:.1f} seconds. "
        f"Output ONLY the Burmese narration text without any English, timestamps, or explanations."
    )

    payload = {
        "contents": [{
            "parts": [
                {"text": prompt},
                {"inline_data": {"mime_type": "audio/mp3", "data": audio_b64}}
            ]
        }]
    }

    last_err = ""
    for model_name in ["gemini-3.8-flash", "gemini-2.5-flash", "gemini-3.5-flash"]:
        try:
            url = f"https://generativelanguage.googleapis.com/v1beta/models/{model_name}:generateContent?key={GEMINI_API_KEY}"
            res = requests.post(url, json=payload, timeout=60)
            if res.status_code == 200:
                data = res.json()
                text = data.get("candidates", [{}])[0].get("content", {}).get("parts", [{}])[0].get("text", "").strip()
                if text:
                    return text
                last_err = f"Empty response from {model_name}: {data}"
            else:
                last_err = f"{model_name} ({res.status_code}): {res.text}"
        except Exception as req_e:
            last_err = str(req_e)

    raise HTTPException(status_code=500, detail=f"Gemini Error: {last_err}")

# --- TELEGRAM BOT ---
tg_app = Application.builder().token(BOT_TOKEN).build() if BOT_TOKEN else None

async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    get_or_create_user(user.id, user.username or user.first_name)
    keyboard = [[InlineKeyboardButton("🎬 Open Movie Myanmar Ai", web_app=WebAppInfo(url=WEBAPP_URL))]]
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
            text=f"🎉 **ငွေပေးချေမှု အတည်ပြုပြီးပါပြီ!**\nOrder: `#{payment_id}` ({plan_name})\n**+{pts} Points** ထည့်သွင်းပေးလိုက်ပါပြီ။",
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
    except Exception as e:
        print(f"Telegram Bot warning: {e}")

@app.on_event("startup")
async def on_startup():
    asyncio.create_task(start_telegram_bot())

# --- FULL PRO UI ---
HTML_UI = """<!DOCTYPE html>
<html lang="my">
<head>
  <meta charset="UTF-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1.0, maximum-scale=1.0, user-scalable=no" />
  <title>Movie Myanmar Ai</title>
  <script src="https://telegram.org/js/telegram-web-app.js"></script>
  <script src="https://cdn.tailwindcss.com"></script>
  <style>
    * { box-sizing: border-box; -webkit-tap-highlight-color: transparent; }
    body { background-color: #0d0e15; color: #f8fafc; font-family: system-ui, -apple-system, sans-serif; }
    .card-dark { background-color: #161822; border: 1px solid #26293a; }
    .sub-card { background-color: #1e2030; border: 1px solid #2d3148; }
    .grad-btn { background: linear-gradient(90deg, #8b5cf6 0%, #38bdf8 50%, #5eead4 100%); color: #090d16; font-weight: 800; }
    .grad-pill { background: linear-gradient(90deg, #8b5cf6 0%, #2dd4bf 100%); color: #090d16; font-weight: 700; }
    .grad-text { background: linear-gradient(90deg, #a78bfa, #38bdf8, #5eead4); -webkit-background-clip: text; -webkit-text-fill-color: transparent; }
    .wave-bar { width: 3px; border-radius: 99px; background: linear-gradient(180deg, #8b5cf6, #2dd4bf); animation: pulse 1.2s infinite ease-in-out; }
    @keyframes pulse { 0%, 100% { transform: scaleY(0.4); } 50% { transform: scaleY(1); } }
    .nav-active { color: #2dd4bf; }
  </style>
</head>
<body class="pb-24 select-none">

  <!-- TOP HEADER -->
  <div class="px-4 pt-3 pb-2 flex items-center justify-between">
    <div class="flex items-center gap-2.5">
      <div class="w-10 h-10 rounded-full bg-gradient-to-tr from-purple-600 via-indigo-500 to-teal-400 flex items-center justify-center shadow-lg">
        <div class="flex items-center gap-0.5 h-4">
          <span class="w-0.5 h-2 bg-white rounded"></span>
          <span class="w-0.5 h-4 bg-white rounded"></span>
          <span class="w-0.5 h-3 bg-white rounded"></span>
          <span class="w-0.5 h-4 bg-white rounded"></span>
          <span class="w-0.5 h-2 bg-white rounded"></span>
        </div>
      </div>
      <div>
        <h1 class="font-extrabold text-base text-white leading-tight">Movie Myanmar Ai</h1>
        <p class="text-xs text-gray-400" id="userGreeting">Hi, K S P</p>
      </div>
    </div>
    <div class="bg-[#1b1d29] border border-gray-800 px-3.5 py-1.5 rounded-full flex items-center gap-1.5 text-xs font-bold">
      <span class="w-2 h-2 rounded-full bg-emerald-400"></span>
      <span id="userPoints" class="text-amber-300">100</span> <span class="text-gray-300">points</span>
    </div>
  </div>

  <!-- HERO BANNER -->
  <div class="px-4 mt-2">
    <div class="card-dark rounded-2xl p-4 space-y-2 relative overflow-hidden">
      <h2 class="text-lg font-extrabold leading-snug">
        ကိုယ်ပိုင်အသံနဲ့ <span class="grad-text">စကားပြောစေမယ်</span>
      </h2>
      <p class="text-xs text-gray-400 leading-relaxed">
        စာရိုက် · အသံနမူနာတင် · စက္ကန့်ပိုင်းအတွင်း အသံထွက်လာမယ်
      </p>
      <div class="flex items-center justify-between h-8 pt-2 px-1 opacity-90" id="waveformBox"></div>
    </div>
  </div>

  <!-- TAB 1: STUDIO -->
  <div id="tab-studio" class="px-4 mt-3 space-y-3">
    <div class="card-dark p-1.5 rounded-2xl grid grid-cols-2 gap-1.5">
      <button onclick="switchMode('tts')" id="modeBtn-tts" class="py-2.5 rounded-xl text-xs font-bold text-gray-300 flex items-center justify-center gap-1.5">
        ✍️ စာ ➔ အသံ
      </button>
      <button onclick="switchMode('video')" id="modeBtn-video" class="py-2.5 rounded-xl text-xs font-extrabold grad-pill flex items-center justify-center gap-1.5 shadow">
        🎬 Video ➔ မြန်မာအသံ
      </button>
    </div>

    <!-- VIDEO -> BURMESE VOICE SECTION -->
    <div id="section-video" class="space-y-3">
      <div class="card-dark rounded-2xl p-4 space-y-3">
        <div>
          <h3 class="font-bold text-base text-white flex items-center gap-1.5">🎬 Video ➔ မြန်မာအသံ</h3>
          <p class="text-xs text-gray-400 mt-1 leading-relaxed">
            English · 中文 · 한국어 · ไทย စတဲ့ ဘာသာစကားမျိုးစုံ video ကို တင်လိုက်ရုံနဲ့ AI က နားထောင်ပြီး မြန်မာလို ဘာသာပြန်၊ မူရင်း Video ကြာချိန်အတိုင်း အချိန်ကွက်တိ အသံထုတ်ပေးပါတယ်။
          </p>
        </div>

        <div class="space-y-2">
          <label class="block w-full py-3 rounded-xl bg-[#222538] border border-dashed border-indigo-400/50 text-center text-xs font-bold text-indigo-300 cursor-pointer active:scale-98">
            📁 နိုင်ငံခြား Video ဖိုင် ရွေးချယ်တင်မည် (MP4/MOV)
            <input type="file" id="videoInput" accept="video/*" class="hidden" onchange="handleVideoUpload(this)" />
          </label>

          <div id="uploadProgressBox" class="hidden bg-[#10121b] p-3 rounded-xl border border-gray-800 space-y-1.5">
            <div class="flex justify-between text-[11px] font-bold">
              <span class="text-cyan-400" id="uploadStatusText">Video တင်နေပါသည်...</span>
              <span class="text-emerald-400" id="uploadPercentText">0%</span>
            </div>
            <div class="w-full h-2.5 bg-gray-800 rounded-full overflow-hidden">
              <div id="uploadBarFill" class="h-full grad-pill transition-all duration-150" style="width: 0%"></div>
            </div>
          </div>
        </div>

        <div class="bg-[#232136] border border-purple-500/20 rounded-xl p-2.5 text-xs text-purple-200">
          🖼️ မူရင်း video ထဲက စာသား/logo ကို ဖုံးချင်ရင် ပုံပေါ်မှာ box ဆွဲပါ (သို့မဟုတ် Video ဘယ်/ညာ လှန်ပါ)
        </div>

        <!-- 3 BUTTONS: ဖုံးမထားဘူး | Blur | ဘယ်/ညာလှန် -->
        <div class="grid grid-cols-3 gap-2">
          <button onclick="setMaskMode('none')" id="mask-none" class="py-2.5 rounded-xl bg-[#1e2030] border border-gray-700 text-xs font-bold text-gray-300">ဖုံးမထားဘူး</button>
          <button onclick="setMaskMode('blur')" id="mask-blur" class="py-2.5 rounded-xl grad-pill text-xs font-extrabold">🌫️ Blur</button>
          <button onclick="toggleFlipVideo()" id="btn-flip" class="py-2.5 rounded-xl bg-[#1e2030] border border-gray-700 text-xs font-bold text-gray-300">🔄 ဘယ်/ညာလှန်</button>
        </div>

        <!-- INTERACTIVE VIDEO PREVIEW -->
        <div id="previewContainer" class="relative w-full rounded-2xl overflow-hidden bg-black border border-gray-800 min-h-[230px] flex items-center justify-center">
          <video id="videoPreview" class="w-full max-h-[360px] object-contain hidden transition-transform duration-200" playsinline controls></video>
          <div id="emptyVideoPlaceholder" class="text-center p-6 space-y-1">
            <p class="text-2xl">🎞️</p>
            <p class="text-xs text-gray-400">အပေါ်မှ Video ရွေးလိုက်ပါက ဤနေရာတွင် Video ပေါ်လာပြီး Blur အကွက်ကို လက်ဖြင့် ဆွဲရွှေ့နိုင်သလို ဘယ်/ညာ လှန်နိုင်ပါမည်</p>
          </div>

          <div id="maskBox" style="top: 62%; left: 10%; width: 80%; height: 14%;"
               class="absolute border-2 border-teal-400 bg-white/15 backdrop-blur-md cursor-move flex items-center justify-center touch-none">
            <span class="text-[10px] font-bold text-white bg-black/50 px-1.5 py-0.5 rounded pointer-events-none">Blur Box</span>
            <div id="resizeHandle" class="absolute -bottom-2.5 -right-2.5 w-6 h-6 rounded-full bg-teal-400 border-2 border-gray-900 shadow-lg cursor-nwse-resize"></div>
          </div>

          <div id="subPosPill" style="bottom: 10%;"
               class="absolute left-1/2 -translate-x-1/2 bg-amber-400 text-gray-950 font-extrabold text-[11px] px-3 py-1 rounded-full shadow-lg cursor-ns-resize whitespace-nowrap touch-none">
            📝 စာတန်း — ဆွဲရွှေ့ပါ
          </div>
        </div>

        <!-- Subtitle Size -->
        <div class="space-y-2 pt-1">
          <div class="sub-card rounded-xl px-3 py-2 text-xs font-bold text-gray-200">
            🔤 စာတန်း အရွယ်အစား <span id="subSizeLabel" class="text-teal-400">ပုံမှန်</span>
          </div>
          <div class="grid grid-cols-5 gap-1.5 text-center">
            <button onclick="setSubSize('0.8x', 'အသေး', 13, this)" class="size-btn py-2 rounded-xl bg-[#1e2030] border border-gray-800 text-[11px] text-gray-300">အသေး<br><span class="text-[10px] text-gray-500">0.8x</span></button>
            <button onclick="setSubSize('0.9x', '0.9x', 15, this)" class="size-btn py-2 rounded-xl bg-[#1e2030] border border-gray-800 text-[11px] text-gray-300">0.9x</button>
            <button onclick="setSubSize('1.0x', 'ပုံမှန်', 17, this)" class="size-btn py-2 rounded-xl grad-pill text-[11px]">ပုံမှန်</button>
            <button onclick="setSubSize('1.15x', '1.15x', 19, this)" class="size-btn py-2 rounded-xl bg-[#1e2030] border border-gray-800 text-[11px] text-gray-300">1.15x</button>
            <button onclick="setSubSize('1.3x', 'အကြီး', 22, this)" class="size-btn py-2 rounded-xl bg-[#1e2030] border border-gray-800 text-[11px] text-gray-300">အကြီး<br><span class="text-[10px] text-gray-500">1.3x</span></button>
          </div>
        </div>

        <!-- Line Spacing -->
        <div class="space-y-2">
          <div class="sub-card rounded-xl px-3 py-2 text-xs font-bold text-gray-200">
            ↕️ စာ ၂ ကြောင်းကြား အကွာ <span id="lineGapLabel" class="text-teal-400">ပုံမှန်</span>
          </div>
          <div class="grid grid-cols-5 gap-1.5 text-center">
            <button onclick="setLineGap('ကျပ်', 1.1, this)" class="gap-btn py-2 rounded-xl bg-[#1e2030] border border-gray-800 text-[11px] text-gray-300">ကျပ်<br><span class="text-[10px] text-gray-500">0.65x</span></button>
            <button onclick="setLineGap('0.8x', 1.25, this)" class="gap-btn py-2 rounded-xl bg-[#1e2030] border border-gray-800 text-[11px] text-gray-300">0.8x</button>
            <button onclick="setLineGap('ပုံမှန်', 1.5, this)" class="gap-btn py-2 rounded-xl grad-pill text-[11px]">ပုံမှန်</button>
            <button onclick="setLineGap('1.2x', 1.75, this)" class="gap-btn py-2 rounded-xl bg-[#1e2030] border border-gray-800 text-[11px] text-gray-300">1.2x</button>
            <button onclick="setLineGap('ကျယ်', 2.0, this)" class="gap-btn py-2 rounded-xl bg-[#1e2030] border border-gray-800 text-[11px] text-gray-300">ကျယ်<br><span class="text-[10px] text-gray-500">1.4x</span></button>
          </div>
        </div>

        <!-- LIVE SUBTITLE PREVIEW BOX -->
        <div class="bg-[#111522] border border-gray-800 rounded-2xl p-4 text-center space-y-2">
          <p id="liveSubSample" style="font-size: 17px; line-height: 1.5;" class="font-bold text-white">
            ဒီနေ့ ကြည့်ရမယ့် <span id="sampleHighlight" class="text-amber-400">ဇာတ်လမ်းလေးက</span><br>တကယ် ကောင်းပါတယ်နော်
          </p>
          <p class="text-[11px] text-gray-500">↑ ထွက်လာမယ့် စာတန်း ပုံစံ နမူနာ</p>
        </div>

        <!-- Aspect Ratio -->
        <div class="space-y-2">
          <div class="sub-card rounded-xl px-3 py-2 text-xs font-bold text-gray-200">📐 Video အချိုးအစား (ratio)</div>
          <div class="grid grid-cols-4 gap-1.5 text-center">
            <button onclick="setRatio('original', this)" class="ratio-btn py-2.5 rounded-xl grad-pill text-[11px]">နဂိုအတိုင်း</button>
            <button onclick="setRatio('9:16', this)" class="ratio-btn py-2.5 rounded-xl bg-[#1e2030] border border-gray-800 text-[11px] text-gray-300">9:16<br><span class="text-[9px] text-gray-400">TikTok</span></button>
            <button onclick="setRatio('1:1', this)" class="ratio-btn py-2.5 rounded-xl bg-[#1e2030] border border-gray-800 text-[11px] text-gray-300">1:1</button>
            <button onclick="setRatio('16:9', this)" class="ratio-btn py-2.5 rounded-xl bg-[#1e2030] border border-gray-800 text-[11px] text-gray-300">16:9</button>
          </div>
        </div>

        <!-- Resolution -->
        <div class="space-y-2">
          <div class="sub-card rounded-xl px-3 py-2 text-xs font-bold text-gray-200">🎞️ Resolution (ကြည်လင်ပြတ်သားမှု)</div>
          <div class="grid grid-cols-5 gap-1.5 text-center">
            <button onclick="setRes('original', this)" class="res-btn py-2 rounded-xl grad-pill text-[11px]">မူရင်း</button>
            <button onclick="setRes('360p', this)" class="res-btn py-2 rounded-xl bg-[#1e2030] border border-gray-800 text-[11px] text-gray-300">360p</button>
            <button onclick="setRes('480p', this)" class="res-btn py-2 rounded-xl bg-[#1e2030] border border-gray-800 text-[11px] text-gray-300">480p</button>
            <button onclick="setRes('720p', this)" class="res-btn py-2 rounded-xl bg-[#1e2030] border border-gray-800 text-[11px] text-gray-300">720p</button>
            <button onclick="setRes('1080p', this)" class="res-btn py-2 rounded-xl bg-[#1e2030] border border-gray-800 text-[11px] text-gray-300">1080p</button>
          </div>
        </div>

        <!-- Subtitle Toggle -->
        <div class="space-y-2">
          <div class="sub-card rounded-xl px-3 py-2 text-xs font-bold text-gray-200">📝 စာတန်း (subtitle) ထိုးမလား?</div>
          <div class="grid grid-cols-2 gap-2">
            <button onclick="setSubEnabled(true, this)" class="suben-btn py-2.5 rounded-xl grad-pill text-xs">✅ စာတန်းထိုးမယ်</button>
            <button onclick="setSubEnabled(false, this)" class="suben-btn py-2.5 rounded-xl bg-[#1e2030] border border-gray-800 text-xs text-gray-300">🚫 မထိုးဘူး (ပိုမြန်တယ်)</button>
          </div>
        </div>

        <!-- Font Style -->
        <div class="space-y-2">
          <div class="sub-card rounded-xl px-3 py-2 text-xs font-bold text-gray-200">🔤 စာတန်း ဖောင့် စတိုင်</div>
          <div class="grid grid-cols-3 gap-2 text-center">
            <button onclick="setFontStyle('sans', this)" class="font-btn py-2.5 rounded-xl grad-pill text-xs">မြန်မာ<br>ရိုးရိုး</button>
            <button onclick="setFontStyle('padauk', this)" class="font-btn py-2.5 rounded-xl bg-[#1e2030] border border-gray-800 text-xs text-gray-300">မြန်မာ<br><span class="text-[10px] text-gray-400">Padauk</span></button>
            <button onclick="setFontStyle('serif', this)" class="font-btn py-2.5 rounded-xl bg-[#1e2030] border border-gray-800 text-xs text-gray-300 font-serif">မြန်မာ<br><span class="text-[10px] text-gray-400">Serif</span></button>
          </div>
        </div>

        <!-- Color Presets -->
        <div class="space-y-2">
          <div class="sub-card rounded-xl px-3 py-2 text-xs font-bold text-gray-200">🎨 စာတန်း အရောင်စတိုင် — ပြောနေတဲ့ စကားလုံးက အရောင်ပြောင်းသွားမယ်</div>
          <div class="grid grid-cols-2 gap-2 text-center text-xs">
            <button onclick="setColorPreset('text-amber-400', this)" class="preset-btn p-2.5 rounded-xl bg-[#1e2030] border-2 border-purple-500">
              <div class="font-bold">ဒီနေ့ <span class="text-amber-400">စာတန်း</span> ပေါ်မယ်</div>
              <div class="text-[10px] text-gray-400 mt-0.5">Clean</div>
            </button>
            <button onclick="setColorPreset('bg-blue-600 text-white px-1 rounded', this)" class="preset-btn p-2.5 rounded-xl bg-[#1e2030] border border-gray-800">
              <div class="font-bold">ဒီနေ့ <span class="bg-blue-600 px-1 rounded">စာတန်း</span> ပေါ်မယ်</div>
              <div class="text-[10px] text-gray-400 mt-0.5">Bold Pop</div>
            </button>
            <button onclick="setColorPreset('text-emerald-400 drop-shadow-[0_0_6px_rgba(52,211,153,0.8)]', this)" class="preset-btn p-2.5 rounded-xl bg-[#1e2030] border border-gray-800">
              <div class="font-bold">ဒီနေ့ <span class="text-emerald-400">စာတန်း</span> ပေါ်မယ်</div>
              <div class="text-[10px] text-gray-400 mt-0.5">Soft Glow</div>
            </button>
            <button onclick="setColorPreset('bg-black/80 text-amber-300 px-1.5 rounded', this)" class="preset-btn p-2.5 rounded-xl bg-[#1e2030] border border-gray-800">
              <div class="font-bold">ဒီနေ့ <span class="bg-black text-amber-300 px-1 rounded">စာတန်း</span> ပေါ်မယ်</div>
              <div class="text-[10px] text-gray-400 mt-0.5">Box</div>
            </button>
            <button onclick="setColorPreset('bg-red-600 text-white px-1 rounded', this)" class="preset-btn p-2.5 rounded-xl bg-[#1e2030] border border-gray-800">
              <div class="font-bold">ဒီနေ့ <span class="bg-red-600 px-1 rounded">စာတန်း</span> ပေါ်မယ်</div>
              <div class="text-[10px] text-gray-400 mt-0.5">Punch</div>
            </button>
            <button onclick="setColorPreset('text-sky-300', this)" class="preset-btn p-2.5 rounded-xl bg-[#1e2030] border border-gray-800">
              <div class="font-bold">ဒီနေ့ <span class="text-sky-300">စာတန်း</span> ပေါ်မယ်</div>
              <div class="text-[10px] text-gray-400 mt-0.5">Calm</div>
            </button>
            <button onclick="setColorPreset('text-yellow-300', this)" class="preset-btn p-2.5 rounded-xl bg-[#1e2030] border border-gray-800">
              <div class="font-bold">ဒီနေ့ <span class="text-yellow-300">စာတန်း</span> ပေါ်မယ်</div>
              <div class="text-[10px] text-gray-400 mt-0.5">Sunny</div>
            </button>
            <button onclick="setColorPreset('bg-pink-600 text-white px-1 rounded', this)" class="preset-btn p-2.5 rounded-xl bg-[#1e2030] border border-gray-800">
              <div class="font-bold">ဒီနေ့ <span class="bg-pink-600 px-1 rounded">စာတန်း</span> ပေါ်မယ်</div>
              <div class="text-[10px] text-gray-400 mt-0.5">Candy</div>
            </button>
          </div>
        </div>

        <!-- FULL-WIDTH AI BUTTON ON TOP + LABEL BELOW -->
        <div class="space-y-2 pt-2">
          <button onclick="autoTranslateVideo()" id="autoTransBtn" class="w-full py-3.5 rounded-xl bg-gradient-to-r from-indigo-600 to-purple-600 font-extrabold text-sm text-white shadow-lg flex items-center justify-center gap-2 active:scale-98">
            ✨ AI Auto ဘာသာပြန် (အမြန်စာသားထုတ်မည်)
          </button>
          <div class="text-xs font-bold text-amber-300 pt-1">
            ✏️ မြန်မာစာတန်းတွေကို ကြိုက်သလို ပြင်ပါ
          </div>
          <textarea id="recapScript" rows="4" placeholder="Video တင်ပြီး အပေါ်က AI Auto ဘာသာပြန် ခလုတ်ကို နှိပ်ပါ (သို့မဟုတ်) မြန်မာစာသား ကိုယ်တိုင် ရိုက်ထည့်ပါ..." class="w-full bg-[#10121b] border border-gray-800 rounded-xl p-3 text-sm text-white focus:border-teal-400 outline-none"></textarea>
        </div>
      </div>
    </div>

    <!-- TEXT -> VOICE SECTION -->
    <div id="section-tts" class="hidden card-dark rounded-2xl p-4 space-y-3">
      <h3 class="font-bold text-sm text-white">✍️ စာသားမှ မြန်မာအသံ ထုတ်မည်</h3>
      <textarea id="ttsOnlyText" rows="5" placeholder="အသံထုတ်လိုသော မြန်မာစာသား ရိုက်ထည့်ပါ..." class="w-full bg-[#10121b] border border-gray-800 rounded-xl p-3 text-sm text-white outline-none"></textarea>
      <button onclick="runTtsOnly()" id="ttsOnlyBtn" class="w-full py-3 rounded-xl grad-btn text-sm shadow-lg">🔊 အသံဖိုင် ထုတ်မည်</button>
      <audio id="ttsOnlyAudio" controls class="w-full hidden"></audio>
    </div>

    <!-- VOICE SELECTION CARDS (WITH WORKING SAMPLE PREVIEW PLAY BUTTONS) -->
    <div class="card-dark rounded-2xl p-4 space-y-3">
      <div class="flex justify-between items-start">
        <div>
          <h3 class="font-bold text-base text-white">🗣️ Voice</h3>
          <p class="text-[11px] text-gray-400 mt-0.5">အသံရွေးပါ (▶ နှိပ်ပြီး အသံအစမ်း နားထောင်ကြည့်နိုင်ပါတယ်)</p>
        </div>
        <button onclick="document.getElementById('cloneModal').classList.remove('hidden')" class="text-[11px] bg-purple-600/30 border border-purple-400/50 text-purple-200 px-2.5 py-1.5 rounded-xl font-bold">
          + Voice Clone
        </button>
      </div>

      <audio id="samplePreviewPlayer" class="hidden"></audio>

      <div class="grid grid-cols-2 gap-2 text-xs">
        <div onclick="selectVoice('maung_thiha', this)" class="voice-card p-3 rounded-xl bg-[#1e2030] border-2 border-purple-500 flex items-center justify-between cursor-pointer">
          <div>
            <div class="font-bold text-white">📖 ပုံပြင်ပြောသူ</div>
            <div class="text-[10px] text-emerald-400">မောင်သီဟ · Free</div>
          </div>
          <button type="button" onclick="playVoiceSample(event, 'maung_thiha', this)" class="w-8 h-8 rounded-full bg-gray-800 hover:bg-teal-500 hover:text-black flex items-center justify-center text-xs font-bold shadow">▶</button>
        </div>

        <div onclick="selectVoice('ko_zaw', this)" class="voice-card p-3 rounded-xl bg-[#1e2030] border border-gray-800 flex items-center justify-between cursor-pointer">
          <div>
            <div class="font-bold text-white">⚽ ဘောလုံး/ဝေဖန်သူ</div>
            <div class="text-[10px] text-emerald-400">ကိုဇော် · Free</div>
          </div>
          <button type="button" onclick="playVoiceSample(event, 'ko_zaw', this)" class="w-8 h-8 rounded-full bg-gray-800 hover:bg-teal-500 hover:text-black flex items-center justify-center text-xs font-bold shadow">▶</button>
        </div>

        <div onclick="selectVoice('u_min', this)" class="voice-card p-3 rounded-xl bg-[#1e2030] border border-gray-800 flex items-center justify-between cursor-pointer">
          <div>
            <div class="font-bold text-white">🎬 ဇာတ်လမ်းပြောသူ</div>
            <div class="text-[10px] text-emerald-400">ဦးမင်း · Free</div>
          </div>
          <button type="button" onclick="playVoiceSample(event, 'u_min', this)" class="w-8 h-8 rounded-full bg-gray-800 hover:bg-teal-500 hover:text-black flex items-center justify-center text-xs font-bold shadow">▶</button>
        </div>

        <div onclick="selectVoice('cloned', this)" class="voice-card p-3 rounded-xl bg-[#1e2030] border border-gray-800 flex items-center justify-between cursor-pointer">
          <div>
            <div class="font-bold text-amber-300">⭐ အသံတူပွား</div>
            <div class="text-[10px] text-gray-400">My Cloned Voice</div>
          </div>
          <button type="button" onclick="playVoiceSample(event, 'cloned', this)" class="w-8 h-8 rounded-full bg-gray-800 hover:bg-amber-400 hover:text-black flex items-center justify-center text-xs font-bold shadow">▶</button>
        </div>
      </div>

      <!-- Speed Selector -->
      <div class="space-y-2 pt-2">
        <div class="sub-card rounded-xl px-3 py-2 text-xs font-bold text-gray-200 flex justify-between">
          <span>🎚️ အသံ အမြန်နှုန်း: <span id="speedBadge" class="text-teal-400">1x</span></span>
        </div>
        <div class="grid grid-cols-6 gap-1 text-center text-[11px]">
          <button onclick="setSpeed(0.8, '0.8x', this)" class="spd-btn py-2 rounded-xl bg-[#1e2030] border border-gray-800">0.8x<br><span class="text-[9px] text-gray-400">နှေး</span></button>
          <button onclick="setSpeed(0.9, '0.9x', this)" class="spd-btn py-2 rounded-xl bg-[#1e2030] border border-gray-800">0.9x</button>
          <button onclick="setSpeed(1.0, '1x', this)" class="spd-btn py-2 rounded-xl grad-pill">1x<br><span class="text-[9px]">ပုံမှန်</span></button>
          <button onclick="setSpeed(1.1, '1.1x', this)" class="spd-btn py-2 rounded-xl bg-[#1e2030] border border-gray-800">1.1x</button>
          <button onclick="setSpeed(1.25, '1.25x', this)" class="spd-btn py-2 rounded-xl bg-[#1e2030] border border-gray-800">1.25x</button>
          <button onclick="setSpeed(1.5, '1.5x', this)" class="spd-btn py-2 rounded-xl bg-[#1e2030] border border-gray-800">1.5x<br><span class="text-[9px] text-gray-400">မြန်</span></button>
        </div>
      </div>
    </div>

    <!-- MAIN RENDER BUTTON + 1% TO 100% PROGRESS BAR + RESULT CARD -->
    <div id="renderSection" class="space-y-3">
      <button onclick="startRecapRender()" id="mainRenderBtn" class="w-full py-4 rounded-2xl grad-btn text-base shadow-xl flex items-center justify-center gap-2 active:scale-98">
        🎬 Recap Video ထုတ်မယ်
      </button>

      <div id="renderProgressCard" class="hidden card-dark rounded-2xl p-4 space-y-3">
        <div class="flex justify-between items-center text-xs font-bold">
          <span class="text-gray-300">🎬 RECAP VIDEO</span>
          <span id="renderPercentLabel" class="text-teal-400 font-extrabold">0%</span>
        </div>
        <div class="w-full h-3 bg-gray-800 rounded-full overflow-hidden">
          <div id="renderBarFill" class="h-full grad-pill transition-all duration-300" style="width: 0%"></div>
        </div>
        <p id="renderStatusMsg" class="text-xs text-gray-300">⏳ Video ကို Render လုပ်နေပါသည် (1% - 100%)...</p>

        <div id="renderDoneActions" class="hidden space-y-2.5 pt-1">
          <p class="text-xs font-bold text-emerald-400">✅ ပြီးပါပြီ — အောက်က ခလုတ်ကို နှိပ်ပါ</p>
          <a id="hdDownloadLink" href="#" download class="block w-full py-3.5 rounded-xl grad-btn text-center text-sm">
            ⬇️ HD ဖိုင် (အရည်အသွေးအပြည့်) ယူမယ်
          </a>
          <button onclick="sendVideoToChat()" id="sendChatBtn" class="w-full py-3.5 rounded-xl bg-[#222538] border border-gray-700 font-bold text-sm text-white">
            📤 Telegram chat ထဲ ပို့မယ်
          </button>
        </div>
      </div>
    </div>
  </div>

  <!-- TAB 2: PLANS -->
  <div id="tab-plans" class="hidden px-4 mt-3 space-y-3">
    <div class="card-dark p-4 rounded-2xl border-teal-500/40 space-y-2">
      <h2 class="font-bold text-sm text-teal-400">💳 ငွေလွှဲလက်ခံမည့် အကောင့်များ</h2>
      <p class="text-xs text-gray-300">အမည်: <b class="text-white">Kaung San Phyo</b></p>
      <div class="flex items-center justify-between bg-[#10121b] p-3 rounded-xl border border-gray-800">
        <div>
          <p class="text-[11px] text-gray-400">KBZPay / WavePay / UABPay</p>
          <p class="font-mono font-bold text-base text-amber-400">09409700002</p>
        </div>
        <button onclick="copyPhone()" class="grad-pill text-xs px-3.5 py-2 rounded-xl">Copy</button>
      </div>
    </div>

    <div onclick="selectPlan('Starter Plan', '15,000 MMK', 1500)" class="card-dark p-4 rounded-2xl flex justify-between items-center cursor-pointer">
      <div><h3 class="font-bold text-sm">Starter Plan</h3><p class="text-xs text-gray-400">1,500 Points</p></div>
      <span class="grad-pill text-xs px-3 py-1.5 rounded-xl">15,000 MMK</span>
    </div>
    <div onclick="selectPlan('Pro Plan', '40,000 MMK', 5000)" class="card-dark p-4 rounded-2xl flex justify-between items-center cursor-pointer border-amber-400/50">
      <div><h3 class="font-bold text-sm text-amber-300">Pro Plan 🔥</h3><p class="text-xs text-gray-400">5,000 Points</p></div>
      <span class="grad-pill text-xs px-3 py-1.5 rounded-xl">40,000 MMK</span>
    </div>
    <div onclick="selectPlan('Business Plan', '88,000 MMK', 12500)" class="card-dark p-4 rounded-2xl flex justify-between items-center cursor-pointer">
      <div><h3 class="font-bold text-sm text-purple-300">Business Plan</h3><p class="text-xs text-gray-400">12,500 Points</p></div>
      <span class="grad-pill text-xs px-3 py-1.5 rounded-xl">88,000 MMK</span>
    </div>

    <div id="paymentBox" class="hidden card-dark p-4 rounded-2xl space-y-3 border-emerald-500/40">
      <p class="text-xs font-bold text-emerald-400">ရွေးထားသော Plan: <span id="selectedPlanLabel"></span></p>
      <input type="file" id="receiptFile" accept="image/*" class="w-full text-xs bg-[#10121b] p-2.5 rounded-xl border border-gray-800" />
      <button onclick="submitPayment()" class="w-full py-3 rounded-xl grad-btn text-xs">✅ ငွေလွှဲပြေစာ ပေးပို့မည်</button>
    </div>
  </div>

  <!-- TAB 3: PROFILE -->
  <div id="tab-profile" class="hidden px-4 mt-3 space-y-3">
    <div class="card-dark p-4 rounded-2xl space-y-2">
      <h2 class="font-bold text-sm text-teal-400">👤 Profile</h2>
      <p class="text-xs text-gray-300">Telegram ID: <span id="profId" class="font-mono text-white"></span></p>
      <p class="text-xs text-gray-300">လက်ကျန် Point: <span id="profPts" class="font-bold text-amber-400"></span></p>
      <p class="text-xs text-gray-300">Tool: <span class="text-white font-bold">Movie Myanmar Ai</span></p>
    </div>
  </div>

  <!-- TAB 4: EARN -->
  <div id="tab-earn" class="hidden px-4 mt-3 space-y-3">
    <div class="card-dark p-5 rounded-2xl text-center space-y-3">
      <div class="text-3xl">🎁</div>
      <h2 class="font-bold text-base">အခမဲ့ Point စုဆောင်းပါ</h2>
      <p class="text-xs text-gray-400">ကြော်ငြာကြည့်ပြီး Point များ အခမဲ့ ရယူနိုင်သလို Free မြန်မာအသံ ၃ မျိုးကို အမြဲ အခမဲ့ သုံးနိုင်ပါသည်။</p>
      <button onclick="earnFreePoints()" class="w-full py-3 rounded-xl grad-btn text-sm">📺 ကြော်ငြာကြည့်ပြီး +5 Points ယူမည်</button>
    </div>
  </div>

  <!-- VOICE CLONE MODAL (MP3/WAV + MP4 VIDEO) -->
  <div id="cloneModal" class="hidden fixed inset-0 bg-black/80 z-50 flex items-center justify-center p-4">
    <div class="card-dark w-full max-w-sm p-5 rounded-2xl space-y-3">
      <h3 class="font-bold text-sm text-purple-300">🎙️ အသံတူပွား ဖန်တီးရန် (Voice Cloning)</h3>
      <p class="text-xs text-gray-400">ဆူညံသံမပါသော ၅ စက္ကန့်မှ ၁၅ စက္ကန့်အတွင်း အသံဖိုင် (MP3/WAV) သို့မဟုတ် Video ဖိုင် (MP4) တင်ပေးပါ။</p>
      <input type="text" id="cloneName" placeholder="အသံအမည် (ဥပမာ- My Voice)" class="w-full bg-[#10121b] border border-gray-800 rounded-xl p-2.5 text-xs text-white" />
      <input type="file" id="cloneSample" accept="audio/*,video/*" class="w-full text-xs bg-[#10121b] border border-gray-800 rounded-xl p-2" />
      <div class="flex gap-2 pt-2">
        <button onclick="document.getElementById('cloneModal').classList.add('hidden')" class="flex-1 py-2.5 rounded-xl bg-gray-800 text-xs">ပိတ်မည်</button>
        <button onclick="uploadVoiceClone()" id="cloneBtn" class="flex-1 py-2.5 rounded-xl grad-pill text-xs">Clone လုပ်မည်</button>
      </div>
    </div>
  </div>

  <!-- BOTTOM NAV -->
  <div class="fixed bottom-0 inset-x-0 bg-[#12131c]/95 backdrop-blur border-t border-gray-800 grid grid-cols-4 text-center py-2 text-[11px] font-bold z-30">
    <button onclick="showTab('studio')" id="nav-studio" class="nav-active flex flex-col items-center gap-0.5"><span>🎙️</span><span>Studio</span></button>
    <button onclick="showTab('plans')" id="nav-plans" class="text-gray-400 flex flex-col items-center gap-0.5"><span>⭐</span><span>Plans</span></button>
    <button onclick="showTab('profile')" id="nav-profile" class="text-gray-400 flex flex-col items-center gap-0.5"><span>👤</span><span>Profile</span></button>
    <button onclick="showTab('earn')" id="nav-earn" class="text-gray-400 flex flex-col items-center gap-0.5"><span>🪙</span><span>Earn</span></button>
  </div>

  <script>
    const tg = window.Telegram.WebApp;
    tg.expand();
    const userId = tg.initDataUnsafe?.user?.id || 1638781080;
    const firstName = tg.initDataUnsafe?.user?.first_name || "K S P";
    document.getElementById("userGreeting").innerText = "Hi, " + firstName;
    document.getElementById("profId").innerText = userId;

    const wf = document.getElementById("waveformBox");
    const heights = [12, 22, 16, 28, 14, 26, 20, 30, 15, 24, 18, 28, 12, 25, 20, 30, 16, 22, 14, 26, 18, 28, 15, 20];
    heights.forEach((h, i) => {
      const b = document.createElement("div");
      b.className = "wave-bar";
      b.style.height = h + "px";
      b.style.animationDelay = (i * 0.08) + "s";
      wf.appendChild(b);
    });

    let state = {
      voice: "maung_thiha",
      speed: 1.0,
      maskMode: "blur",
      flipH: false,
      ratio: "original",
      res: "original",
      subEnabled: true,
      lastVideoUrl: "",
      selectedPlan: {}
    };

    async function loadUser() {
      try {
        const res = await fetch(`/api/user/${userId}`);
        const data = await res.json();
        document.getElementById("userPoints").innerText = data.points;
        document.getElementById("profPts").innerText = data.points + " points";
      } catch(e) {}
    }
    loadUser();

    function showTab(tab) {
      ["studio", "plans", "profile", "earn"].forEach(t => {
        document.getElementById("tab-" + t).classList.add("hidden");
        document.getElementById("nav-" + t).classList.remove("nav-active");
        document.getElementById("nav-" + t).classList.add("text-gray-400");
      });
      document.getElementById("tab-" + tab).classList.remove("hidden");
      document.getElementById("nav-" + tab).classList.add("nav-active");
      document.getElementById("nav-" + tab).classList.remove("text-gray-400");
    }

    function switchMode(m) {
      if (m === "tts") {
        document.getElementById("section-tts").classList.remove("hidden");
        document.getElementById("section-video").classList.add("hidden");
        document.getElementById("renderSection").classList.add("hidden");
        document.getElementById("modeBtn-tts").className = "py-2.5 rounded-xl text-xs font-extrabold grad-pill flex items-center justify-center gap-1.5 shadow";
        document.getElementById("modeBtn-video").className = "py-2.5 rounded-xl text-xs font-bold text-gray-300 flex items-center justify-center gap-1.5";
      } else {
        document.getElementById("section-video").classList.remove("hidden");
        document.getElementById("renderSection").classList.remove("hidden");
        document.getElementById("section-tts").classList.add("hidden");
        document.getElementById("modeBtn-video").className = "py-2.5 rounded-xl text-xs font-extrabold grad-pill flex items-center justify-center gap-1.5 shadow";
        document.getElementById("modeBtn-tts").className = "py-2.5 rounded-xl text-xs font-bold text-gray-300 flex items-center justify-center gap-1.5";
      }
    }

    function handleVideoUpload(input) {
      const file = input.files[0];
      if (!file) return;

      const progBox = document.getElementById("uploadProgressBox");
      const fill = document.getElementById("uploadBarFill");
      const pctText = document.getElementById("uploadPercentText");
      const statusText = document.getElementById("uploadStatusText");

      progBox.classList.remove("hidden");
      let pct = 1;
      const timer = setInterval(() => {
        pct += Math.floor(Math.random() * 14) + 6;
        if (pct >= 100) {
          pct = 100;
          clearInterval(timer);
          statusText.innerText = "✅ Video တင်ပြီးပါပြီ (" + file.name + ")";
          const vid = document.getElementById("videoPreview");
          vid.src = URL.createObjectURL(file);
          vid.classList.remove("hidden");
          document.getElementById("emptyVideoPlaceholder").classList.add("hidden");
        }
        fill.style.width = pct + "%";
        pctText.innerText = pct + "%";
      }, 60);
    }

    function setMaskMode(mode) {
      state.maskMode = mode;
      const box = document.getElementById("maskBox");
      ["none", "blur"].forEach(m => {
        document.getElementById("mask-" + m).className = "py-2.5 rounded-xl bg-[#1e2030] border border-gray-700 text-xs font-bold text-gray-300";
      });
      document.getElementById("mask-" + mode).className = "py-2.5 rounded-xl grad-pill text-xs font-extrabold";

      if (mode === "none") {
        box.classList.add("hidden");
      } else {
        box.classList.remove("hidden");
        box.style.backgroundColor = "rgba(255,255,255,0.15)";
        box.style.backdropFilter = "blur(10px)";
      }
    }

    function toggleFlipVideo() {
      state.flipH = !state.flipH;
      const btn = document.getElementById("btn-flip");
      const vid = document.getElementById("videoPreview");
      if (state.flipH) {
        btn.className = "py-2.5 rounded-xl grad-pill text-xs font-extrabold";
        vid.style.transform = "scaleX(-1)";
      } else {
        btn.className = "py-2.5 rounded-xl bg-[#1e2030] border border-gray-700 text-xs font-bold text-gray-300";
        vid.style.transform = "scaleX(1)";
      }
    }

    const container = document.getElementById("previewContainer");
    const maskBox = document.getElementById("maskBox");
    const resizeHandle = document.getElementById("resizeHandle");
    const subPill = document.getElementById("subPosPill");

    let dragTarget = null, startX = 0, startY = 0, startLeft = 0, startTop = 0, startW = 0, startH = 0;

    function getClientXY(e) {
      return e.touches ? { x: e.touches[0].clientX, y: e.touches[0].clientY } : { x: e.clientX, y: e.clientY };
    }

    maskBox.addEventListener("mousedown", startDrag);
    maskBox.addEventListener("touchstart", startDrag, { passive: false });
    resizeHandle.addEventListener("mousedown", startResize);
    resizeHandle.addEventListener("touchstart", startResize, { passive: false });
    subPill.addEventListener("mousedown", startSubDrag);
    subPill.addEventListener("touchstart", startSubDrag, { passive: false });

    function startDrag(e) {
      if (e.target === resizeHandle) return;
      e.preventDefault();
      dragTarget = "move";
      const pt = getClientXY(e);
      startX = pt.x; startY = pt.y;
      startLeft = maskBox.offsetLeft; startTop = maskBox.offsetTop;
    }
    function startResize(e) {
      e.stopPropagation(); e.preventDefault();
      dragTarget = "resize";
      const pt = getClientXY(e);
      startX = pt.x; startY = pt.y;
      startW = maskBox.offsetWidth; startH = maskBox.offsetHeight;
    }
    function startSubDrag(e) {
      e.preventDefault();
      dragTarget = "sub";
      const pt = getClientXY(e);
      startY = pt.y;
      startTop = subPill.offsetTop;
    }

    window.addEventListener("mousemove", onMove);
    window.addEventListener("touchmove", onMove, { passive: false });
    window.addEventListener("mouseup", () => dragTarget = null);
    window.addEventListener("touchend", () => dragTarget = null);

    function onMove(e) {
      if (!dragTarget) return;
      const pt = getClientXY(e);
      const cw = container.clientWidth, ch = container.clientHeight;
      if (dragTarget === "move") {
        let nl = Math.max(0, Math.min(cw - maskBox.offsetWidth, startLeft + (pt.x - startX)));
        let nt = Math.max(0, Math.min(ch - maskBox.offsetHeight, startTop + (pt.y - startY)));
        maskBox.style.left = (nl / cw * 100) + "%";
        maskBox.style.top = (nt / ch * 100) + "%";
      } else if (dragTarget === "resize") {
        let nw = Math.max(40, Math.min(cw - maskBox.offsetLeft, startW + (pt.x - startX)));
        let nh = Math.max(20, Math.min(ch - maskBox.offsetTop, startH + (pt.y - startY)));
        maskBox.style.width = (nw / cw * 100) + "%";
        maskBox.style.height = (nh / ch * 100) + "%";
      } else if (dragTarget === "sub") {
        let nt = Math.max(10, Math.min(ch - 36, startTop + (pt.y - startY)));
        subPill.style.bottom = "auto";
        subPill.style.top = (nt / ch * 100) + "%";
      }
    }

    function activateGroup(cls, el) {
      document.querySelectorAll("." + cls).forEach(b => {
        b.className = b.className.replace("grad-pill", "bg-[#1e2030] border border-gray-800 text-gray-300");
      });
      el.className = el.className.replace("bg-[#1e2030] border border-gray-800 text-gray-300", "grad-pill");
    }

    function setSubSize(code, label, px, el) {
      activateGroup("size-btn", el);
      document.getElementById("subSizeLabel").innerText = label;
      document.getElementById("liveSubSample").style.fontSize = px + "px";
    }
    function setLineGap(label, lh, el) {
      activateGroup("gap-btn", el);
      document.getElementById("lineGapLabel").innerText = label;
      document.getElementById("liveSubSample").style.lineHeight = lh;
    }
    function setRatio(r, el) { state.ratio = r; activateGroup("ratio-btn", el); }
    function setRes(r, el) { state.res = r; activateGroup("res-btn", el); }
    function setSubEnabled(val, el) {
      state.subEnabled = val;
      activateGroup("suben-btn", el);
      subPill.style.display = val ? "block" : "none";
    }
    function setFontStyle(style, el) {
      activateGroup("font-btn", el);
      const s = document.getElementById("liveSubSample");
      s.style.fontFamily = style === "serif" ? "serif" : "sans-serif";
    }
    function setColorPreset(cls, el) {
      document.querySelectorAll(".preset-btn").forEach(b => b.classList.remove("border-2", "border-purple-500"));
      el.classList.add("border-2", "border-purple-500");
      document.getElementById("sampleHighlight").className = cls;
    }
    function selectVoice(v, el) {
      state.voice = v;
      document.querySelectorAll(".voice-card").forEach(c => {
        c.classList.remove("border-2", "border-purple-500");
        c.classList.add("border", "border-gray-800");
      });
      el.classList.remove("border", "border-gray-800");
      el.classList.add("border-2", "border-purple-500");
    }
    function setSpeed(val, label, el) {
      state.speed = val;
      document.getElementById("speedBadge").innerText = label;
      activateGroup("spd-btn", el);
    }

    // PLAY VOICE SAMPLE PREVIEW
    let activePlayBtn = null;
    async function playVoiceSample(event, voiceKey, btn) {
      event.stopPropagation();
      const player = document.getElementById("samplePreviewPlayer");

      if (activePlayBtn && activePlayBtn !== btn) {
        activePlayBtn.innerText = "▶";
      }
      if (!player.paused && activePlayBtn === btn) {
        player.pause();
        btn.innerText = "▶";
        return;
      }

      activePlayBtn = btn;
      btn.innerText = "⏳";

      try {
        const res = await fetch(`/api/voice-sample/${voiceKey}?user_id=${userId}`);
        const rawText = await res.text();
        let data = {};
        try { data = JSON.parse(rawText); } catch(e) { throw new Error(rawText); }
        if (!res.ok) throw new Error(data.detail || "အသံနမူနာ ဖွင့်မရပါ");

        player.src = data.audio_url;
        btn.innerText = "⏸";
        await player.play();
        player.onended = () => { btn.innerText = "▶"; };
      } catch (err) {
        btn.innerText = "▶";
        alert(err.message);
      }
    }

    // FAST AUTO AI TRANSLATE
    async function autoTranslateVideo() {
      const file = document.getElementById("videoInput").files[0];
      if (!file) return alert("Video ဖိုင် အရင်ရွေးပါ။");
      const btn = document.getElementById("autoTransBtn");
      btn.innerText = "⚡ စာသား အမြန်ထုတ်နေပါသည်...";
      btn.disabled = true;

      const fd = new FormData();
      fd.append("video", file);
      try {
        const res = await fetch("/api/auto-script", { method: "POST", body: fd });
        const rawText = await res.text();
        let data = {};
        try { data = JSON.parse(rawText); } catch(e) { throw new Error(rawText); }
        if (!res.ok) throw new Error(data.detail || "Error");
        document.getElementById("recapScript").value = data.script;
      } catch (e) {
        alert(e.message);
      } finally {
        btn.innerText = "✨ AI Auto ဘာသာပြန် (အမြန်စာသားထုတ်မည်)";
        btn.disabled = false;
      }
    }

    // START RECAP RENDER
    async function startRecapRender() {
      const file = document.getElementById("videoInput").files[0];
      const script = document.getElementById("recapScript").value.trim();
      if (!file) return alert("Video ဖိုင် အရင်ရွေးပါ။");
      if (!script) return alert("မြန်မာစာသား ရိုက်ထည့်ပါ (သို့) AI Auto ဘာသာပြန် နှိပ်ပါ။");

      const card = document.getElementById("renderProgressCard");
      const fill = document.getElementById("renderBarFill");
      const pctLabel = document.getElementById("renderPercentLabel");
      const statusMsg = document.getElementById("renderStatusMsg");
      const doneActions = document.getElementById("renderDoneActions");

      card.classList.remove("hidden");
      doneActions.classList.add("hidden");
      card.scrollIntoView({ behavior: "smooth" });

      let pct = 1;
      const timer = setInterval(() => {
        if (pct < 92) {
          pct += Math.floor(Math.random() * 4) + 1;
          if (pct > 92) pct = 92;
          fill.style.width = pct + "%";
          pctLabel.innerText = pct + "%";
          statusMsg.innerText = `⏳ Video ကြာချိန်နှင့် အသံကို အတိအကျ ညှိ၍ Render လုပ်နေပါသည် (${pct}%)...`;
        }
      }, 600);

      const cw = container.clientWidth || 300;
      const ch = container.clientHeight || 200;
      const boxX = Math.round((maskBox.offsetLeft / cw) * 100);
      const boxY = Math.round((maskBox.offsetTop / ch) * 100);
      const boxW = Math.round((maskBox.offsetWidth / cw) * 100);
      const boxH = Math.round((maskBox.offsetHeight / ch) * 100);

      const fd = new FormData();
      fd.append("user_id", userId);
      fd.append("script_text", script);
      fd.append("voice_type", state.voice);
      fd.append("speed", state.speed);
      fd.append("mask_mode", state.maskMode);
      fd.append("flip_h", state.flipH ? "1" : "0");
      fd.append("box_x", boxX);
      fd.append("box_y", boxY);
      fd.append("box_w", boxW);
      fd.append("box_h", boxH);
      fd.append("aspect_ratio", state.ratio);
      fd.append("video", file);

      try {
        const res = await fetch("/api/render-recap", { method: "POST", body: fd });
        const rawText = await res.text();
        let data = {};
        try { data = JSON.parse(rawText); } catch(e) { throw new Error(rawText); }
        clearInterval(timer);
        if (!res.ok) throw new Error(data.detail || "Render failed");

        fill.style.width = "100%";
        pctLabel.innerText = "100%";
        statusMsg.innerText = "🎉 Render 100% ပြီးစီးပါပြီ! (မူရင်း Video ကြာချိန်အတိုင်း ထွက်လာပါပြီ)";
        state.lastVideoUrl = data.video_url;
        document.getElementById("hdDownloadLink").href = data.video_url;
        doneActions.classList.remove("hidden");
        loadUser();
      } catch (err) {
        clearInterval(timer);
        statusMsg.innerText = "❌ Error: " + err.message;
      }
    }

    async function sendVideoToChat() {
      if (!state.lastVideoUrl) return;
      const btn = document.getElementById("sendChatBtn");
      btn.innerText = "⏳ Telegram Chat ထဲ ပို့နေပါသည်...";
      const fd = new FormData();
      fd.append("user_id", userId);
      fd.append("video_url", state.lastVideoUrl);
      const res = await fetch("/api/send-to-telegram", { method: "POST", body: fd });
      btn.innerText = "📤 Telegram chat ထဲ ပို့မယ်";
      if (res.ok) alert("✅ သင့် Telegram Bot Chat ထဲသို့ Video ပို့ပေးလိုက်ပါပြီ!");
    }

    async function runTtsOnly() {
      const text = document.getElementById("ttsOnlyText").value.trim();
      if (!text) return alert("စာသားရိုက်ထည့်ပါ။");
      const btn = document.getElementById("ttsOnlyBtn");
      btn.innerText = "⏳ အသံထုတ်နေပါသည်...";
      const fd = new FormData();
      fd.append("user_id", userId);
      fd.append("text", text);
      fd.append("voice_type", state.voice);
      fd.append("speed", state.speed);
      const res = await fetch("/api/tts", { method: "POST", body: fd });
      const data = await res.json();
      btn.innerText = "🔊 အသံဖိုင် ထုတ်မည်";
      if (!res.ok) return alert(data.detail);
      const aud = document.getElementById("ttsOnlyAudio");
      aud.src = data.audio_url;
      aud.classList.remove("hidden");
      aud.play();
      loadUser();
    }

    async function uploadVoiceClone() {
      const name = document.getElementById("cloneName").value.trim();
      const file = document.getElementById("cloneSample").files[0];
      if (!name || !file) return alert("အမည်နှင့် အသံ/Video ဖိုင် ထည့်ပါ။");
      const btn = document.getElementById("cloneBtn");
      btn.innerText = "⏳ အသံခွဲထုတ်ပြီး Clone လုပ်နေသည်...";
      btn.disabled = true;

      const fd = new FormData();
      fd.append("user_id", userId);
      fd.append("voice_name", name);
      fd.append("sample", file);
      const res = await fetch("/api/clone-voice", { method: "POST", body: fd });
      const data = await res.json();
      btn.innerText = "Clone လုပ်မည်";
      btn.disabled = false;
      if (!res.ok) return alert(data.detail);
      alert("✅ အသံတူပွား ဖန်တီးပြီးပါပြီ!");
      document.getElementById("cloneModal").classList.add("hidden");
    }

    function copyPhone() {
      navigator.clipboard.writeText("09409700002");
      alert("09409700002 (Kaung San Phyo) ကို Copy ကူးပြီးပါပြီ။");
    }
    function selectPlan(name, amount, pts) {
      state.selectedPlan = { name, amount, pts };
      document.getElementById("selectedPlanLabel").innerText = `${name} (${amount} - ${pts} pts)`;
      document.getElementById("paymentBox").classList.remove("hidden");
    }
    async function submitPayment() {
      const file = document.getElementById("receiptFile").files[0];
      if (!file) return alert("ပြေစာပုံ ရွေးပါ။");
      const fd = new FormData();
      fd.append("user_id", userId);
      fd.append("plan_name", state.selectedPlan.name);
      fd.append("amount", state.selectedPlan.amount);
      fd.append("points", state.selectedPlan.pts);
      fd.append("receipt", file);
      const res = await fetch("/api/submit-payment", { method: "POST", body: fd });
      if (res.ok) {
        alert("✅ ပြေစာ ပေးပို့ပြီးပါပြီ!");
        document.getElementById("paymentBox").classList.add("hidden");
      }
    }
    async function earnFreePoints() {
      const fd = new FormData();
      fd.append("user_id", userId);
      const res = await fetch("/api/earn-ad", { method: "POST", body: fd });
      if (res.ok) { alert("🎉 +5 Points ရရှိပါပြီ!"); loadUser(); }
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

@app.get("/api/voice-sample/{voice_key}")
async def api_voice_sample(voice_key: str, user_id: int = 1638781080):
    if voice_key == "cloned":
        user = get_or_create_user(user_id)
        if not user["cloned_voice_id"]:
            raise HTTPException(status_code=400, detail="အသံတူပွား (Voice Clone) အရင်ဖန်တီးပြီးမှ နားထောင်နိုင်ပါသည်။")
        out_path = f"outputs/sample_cloned_{user_id}.mp3"
        if not os.path.exists(out_path):
            generate_cloned_tts("မင်္ဂလာပါ။ ဒါကတော့ သင့်ရဲ့ ကိုယ်ပိုင် အသံတူပွား နမူနာ ဖြစ်ပါတယ်။", user["cloned_voice_id"], out_path)
        return {"audio_url": f"/{out_path}"}

    if voice_key not in FREE_VOICES:
        raise HTTPException(status_code=404, detail="Voice not found")

    out_path = f"outputs/sample_{voice_key}.mp3"
    if not os.path.exists(out_path):
        await generate_free_burmese_tts(FREE_VOICES[voice_key]["sample"], voice_key, 1.0, out_path)
    return {"audio_url": f"/{out_path}"}

@app.post("/api/tts")
async def api_tts(user_id: int = Form(...), text: str = Form(...), voice_type: str = Form(...), speed: float = Form(1.0)):
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

    return {"audio_url": f"/outputs/tts_{file_id}.mp3"}

@app.post("/api/clone-voice")
async def api_clone_voice(user_id: int = Form(...), voice_name: str = Form(...), sample: UploadFile = File(...)):
    job_id = str(uuid.uuid4())[:8]
    raw_path = f"uploads/{job_id}_{sample.filename}"
    audio_path = f"uploads/{job_id}_extracted.mp3"

    with open(raw_path, "wb") as f:
        f.write(await sample.read())

    if not has_audio_stream(raw_path):
        raise HTTPException(status_code=400, detail="တင်လိုက်သော ဖိုင်ထဲတွင် အသံ (Audio) မပါဝင်ပါ။")

    subprocess.run(
        ["ffmpeg", "-y", "-i", raw_path, "-vn", "-acodec", "libmp3lame", "-q:a", "2", audio_path],
        check=True
    )

    voice_id = clone_voice_elevenlabs(user_id, voice_name, audio_path)
    return {"status": "success", "voice_id": voice_id}

@app.post("/api/auto-script")
async def api_auto_script(video: UploadFile = File(...)):
    job_id = str(uuid.uuid4())[:8]
    in_vid = f"uploads/tmp_{job_id}_{video.filename}"
    in_aud = f"uploads/tmp_{job_id}.mp3"

    try:
        with open(in_vid, "wb") as f:
            f.write(await video.read())

        if not has_audio_stream(in_vid):
            raise HTTPException(status_code=400, detail="ဤ Video ဖိုင်ထဲတွင် စကားပြောအသံ (Audio) လုံးဝ မပါဝင်ပါ။ မြန်မာစာသားကို ကိုယ်တိုင် ရိုက်ထည့်ပေးပါ။")

        vid_dur = get_media_duration(in_vid)
        res = subprocess.run(
            ["ffmpeg", "-y", "-i", in_vid, "-vn", "-ac", "1", "-ar", "16000", "-b:a", "32k", in_aud],
            capture_output=True, text=True
        )
        if res.returncode != 0 or not os.path.exists(in_aud):
            raise HTTPException(status_code=500, detail=f"Audio ခွဲထုတ်မရပါ: {res.stderr[-200:]}")

        script = call_gemini_audio(in_aud, vid_dur)
        return {"script": script}
    except HTTPException as he:
        raise he
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Auto Script Error: {str(e)}")

@app.post("/api/render-recap")
async def api_render_recap(
    user_id: int = Form(...),
    script_text: str = Form(...),
    voice_type: str = Form("maung_thiha"),
    speed: float = Form(1.0),
    mask_mode: str = Form("blur"),
    flip_h: str = Form("0"),
    box_x: int = Form(10),
    box_y: int = Form(62),
    box_w: int = Form(80),
    box_h: int = Form(14),
    aspect_ratio: str = Form("original"),
    video: UploadFile = File(...)
):
    try:
        user = get_or_create_user(user_id)
        job_id = str(uuid.uuid4())[:8]
        in_vid = f"uploads/in_{job_id}_{video.filename}"
        raw_tts = f"outputs/raw_audio_{job_id}.mp3"
        synced_tts = f"outputs/recap_audio_{job_id}.mp3"
        out_vid = f"outputs/MovieMyanmarAi_{job_id}.mp4"

        with open(in_vid, "wb") as f:
            f.write(await video.read())

        vid_dur = get_media_duration(in_vid)
        vid_has_audio = has_audio_stream(in_vid)

        if voice_type == "cloned" and user["cloned_voice_id"] and user["points"] >= 10:
            generate_cloned_tts(script_text, user["cloned_voice_id"], raw_tts)
            update_points(user_id, -10)
        else:
            await generate_free_burmese_tts(script_text, voice_type if voice_type != "cloned" else "maung_thiha", speed, raw_tts)

        tts_dur = get_media_duration(raw_tts)
        if vid_dur > 0 and tts_dur > 0:
            ratio = tts_dur / vid_dur
            tempo = max(0.75, min(1.45, ratio))
            subprocess.run(
                ["ffmpeg", "-y", "-i", raw_tts, "-filter:a", f"atempo={tempo:.3f}", synced_tts],
                check=True
            )
        else:
            synced_tts = raw_tts

        filters = []
        if flip_h == "1":
            filters.append("hflip")

        if mask_mode == "blur" and box_w > 0 and box_h > 0:
            filters.append(
                f"split[main][b];[b]crop=iw*{box_w/100}:ih*{box_h/100}:iw*{box_x/100}:ih*{box_y/100},boxblur=15:5[blurred];"
                f"[main][blurred]overlay=W*{box_x/100}:H*{box_y/100}"
            )

        if aspect_ratio == "9:16":
            filters.append("scale=720:1280:force_original_aspect_ratio=decrease,pad=720:1280:(ow-iw)/2:(oh-ih)/2")
        elif aspect_ratio == "1:1":
            filters.append("scale=720:720:force_original_aspect_ratio=decrease,pad=720:720:(ow-iw)/2:(oh-ih)/2")

        vf_str = ",".join(filters) if filters else "null"

        if vid_has_audio:
            filter_complex = f"[0:v]{vf_str}[v];[0:a]volume=0.12[bg];[1:a]volume=1.35[fg];[bg][fg]amix=inputs=2:duration=first[a]"
        else:
            filter_complex = f"[0:v]{vf_str}[v];[1:a]volume=1.35[a]"

        cmd = [
            "ffmpeg", "-y", "-i", in_vid, "-i", synced_tts,
            "-filter_complex", filter_complex,
            "-map", "[v]", "-map", "[a]", "-c:v", "libx264", "-preset", "ultrafast", "-c:a", "aac", "-shortest", out_vid
        ]
        subprocess.run(cmd, check=True)

        return {"video_url": f"/{out_vid}"}
    except HTTPException as he:
        raise he
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Render Error: {str(e)}")

@app.post("/api/send-to-telegram")
async def api_send_to_tg(user_id: int = Form(...), video_url: str = Form(...)):
    file_path = video_url.lstrip("/")
    if not os.path.exists(file_path):
        raise HTTPException(status_code=404, detail="Video ဖိုင် မတွေ့ပါ။")
    if tg_app:
        with open(file_path, "rb") as vf:
            await tg_app.bot.send_video(
                chat_id=user_id,
                video=vf,
                caption="🎬 **Movie Myanmar Ai** မှ Recap Video ပေးပို့လိုက်ပါပြီ!",
                parse_mode="Markdown"
            )
    return {"status": "sent"}

@app.post("/api/submit-payment")
async def api_submit_payment(user_id: int = Form(...), plan_name: str = Form(...), amount: str = Form(...), points: int = Form(...), receipt: UploadFile = File(...)):
    conn = sqlite3.connect("movie_myanmar_ai.db")
    c = conn.cursor()
    c.execute("INSERT INTO payments (user_id, plan_name, amount, points) VALUES (?, ?, ?, ?)", (user_id, plan_name, amount, points))
    payment_id = c.lastrowid
    conn.commit()
    conn.close()

    img_path = f"uploads/receipt_{payment_id}_{receipt.filename}"
    with open(img_path, "wb") as f:
        f.write(await receipt.read())

    if tg_app:
        kb = [[InlineKeyboardButton("✅ Approve", callback_data=f"approve_{payment_id}"),
               InlineKeyboardButton("❌ Reject", callback_data=f"reject_{payment_id}")]]
        with open(img_path, "rb") as photo:
            await tg_app.bot.send_photo(
                chat_id=ADMIN_ID,
                photo=photo,
                caption=f"💰 **ငွေလွှဲပြေစာ အသစ် (#{payment_id})**\n👤 User ID: `{user_id}`\n📦 Plan: **{plan_name}** ({amount})\n💎 Points: **+{points} pts**",
                reply_markup=InlineKeyboardMarkup(kb),
                parse_mode="Markdown"
            )
    return {"status": "submitted"}

@app.post("/api/earn-ad")
async def api_earn_ad(user_id: int = Form(...)):
    update_points(user_id, 5)
    return {"points": get_or_create_user(user_id)["points"]}

@app.get("/outputs/{filename}")
async def get_output(filename: str):
    return FileResponse(f"outputs/{filename}")
