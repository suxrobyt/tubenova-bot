"""TubeNova AI — YouTuberlar uchun bepul Telegram bot (aiogram 3 + Groq + ffmpeg)."""
import asyncio
import io
import json
import logging
import os
import random
import re
import secrets
import tempfile
from collections import defaultdict
from datetime import datetime
from zoneinfo import ZoneInfo

import aiosqlite
from aiohttp import web
from aiogram import Bot, Dispatcher, F, Router
from aiogram.filters import CommandObject, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (BufferedInputFile, CallbackQuery, FSInputFile, InlineKeyboardButton,
                           InlineKeyboardMarkup, KeyboardButton, Message, ReplyKeyboardMarkup)
from dotenv import load_dotenv
from groq import AsyncGroq
from PIL import Image

import analysis
import coffin
import media
import render
import swapper

load_dotenv()
logging.basicConfig(level=logging.INFO)
log = logging.getLogger("tubenova")

# ---------- Sozlamalar (Railway Variables yoki .env) ----------
BOT_TOKEN = os.environ["BOT_TOKEN"]
GROQ_API_KEY = os.environ["GROQ_API_KEY"]
MODEL = os.getenv("MODEL", "openai/gpt-oss-120b")
ADMIN_USERNAME = os.getenv("ADMIN_USERNAME", "")       # @ belgisisiz
ADMIN_ID = int(os.getenv("ADMIN_ID", "0"))
DAILY_Q = int(os.getenv("DAILY_QUESTIONS", "15"))
DAILY_S = int(os.getenv("DAILY_SERVICES", "3"))
REF_BONUS = int(os.getenv("REF_BONUS", "2"))
# Cheklovsiz foydalanuvchilar: ADMIN_ID + UNLIMITED_IDS (vergul bilan, raqamlar)
UNLIMITED = {int(x) for x in os.getenv("UNLIMITED_IDS", "").replace(" ", "").split(",") if x.isdigit()}
if ADMIN_ID:
    UNLIMITED.add(ADMIN_ID)
DB = "tubenova.db"
TZ = ZoneInfo("Asia/Tashkent")
N = 4                      # Tiles Hop: rasm va video soni
TG_API_ID = os.getenv("TG_API_ID", "")        # ixtiyoriy: katta fayllar (MTProto) uchun
TG_API_HASH = os.getenv("TG_API_HASH", "")
tg_client = None

ai = AsyncGroq(api_key=GROQ_API_KEY)
router = Router()
BOT_USERNAME = ""
ORDER_LOCKS = defaultdict(asyncio.Lock)
RENDERING = set()                    # hozir video tayyorlanayotgan foydalanuvchilar
RENDER_SEM = asyncio.Semaphore(1)    # bir vaqtda bitta video (server xotirasi uchun)
BG = set()                           # fon vazifalari (o'chib ketmasligi uchun ushlab turiladi)
MAX_SHOTS = 5


def max_mb() -> int:
    """Qabul qilinadigan fayl hajmi: oddiy bot 20 MB, MTProto yoqilgan bo'lsa 200 MB."""
    return 200 if tg_client is not None else 20


def spawn(coro):
    """Vazifani fonda ishga tushiradi: foydalanuvchi kutish paytida botning boshqa bo'limlarini ishlata oladi."""
    task = asyncio.create_task(coro)
    BG.add(task)
    task.add_done_callback(BG.discard)


async def health(request):
    return web.Response(text="OK")


async def start_web():
    app = web.Application()
    app.router.add_get("/", health)
    runner = web.AppRunner(app)
    await runner.setup()
    port = int(os.environ.get("PORT", 10000))
    await web.TCPSite(runner, "0.0.0.0", port).start()


# ---------- Tugmalar ----------
BTN_QA = "💬 Savol-Javob 24/7"
BTN_SERVICES = "🛍 Barcha xizmatlar"
BTN_CHANNEL = "📺 Kanal ochish"
BTN_VIDEO = "🎬 Video tayyorlash"
BTN_LIMITS = "🎁 Bepul imkoniyatlarim"
BTN_REF = "👥 Do'st taklif qilish"
BTN_TOS = "📜 Foydalanish shartlari"
BTN_HELP = "🆘 Yordam olish"
BTN_MAIN = "↩️ Asosiy menyu"
BTN_V_MAKE = "▶️ Videoni tayyorlash"
BTN_V_FILES = "📋 Yuborilgan fayllarni ko'rish"
BTN_V_CANCEL = "❌ Buyurtmani bekor qilish"
BTN_V_NEW = "🔄 Yangi "
BTN_SWAP = "🔀 Video joyini almashtirish"
BTN_ANALYSIS = "📊 YouTube tahlil"
BTN_A_RUN = "👁 Tahlil qilish"
BTN_A_NEW = "🆕 Yangi tahlil"
BTN_A_CANCEL = "❌ Bekor qilish"

DIRECTIONS = ["🎮 Tiles Hop", "🕺 Coffin Dance", "👽 Alien Dance",
              "👾 Tiles Hop EXE", "🏆 GOLDEN COVER", "🎤 Say The Word", "👀 Wrong Eyes"]
SWAP_DIR = BTN_SWAP
READY = {"Tiles Hop", "Coffin Dance", "Video joyini almashtirish"}   # ishlaydigan yo'nalishlar


def kb(rows):
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text=t) for t in r] for r in rows], resize_keyboard=True)


MAIN_KB = kb([[BTN_QA, BTN_SERVICES], [BTN_LIMITS, BTN_REF], [BTN_TOS, BTN_HELP]])
SERVICES_KB = kb([[BTN_CHANNEL, BTN_VIDEO], [BTN_SWAP], [BTN_MAIN]])
VIDEO_KB = kb([DIRECTIONS[i:i + 2] for i in range(0, len(DIRECTIONS), 2)] + [[BTN_SERVICES], [BTN_MAIN]])
ANALYSIS_KB = kb([[BTN_A_RUN, BTN_A_NEW], [BTN_A_CANCEL], [BTN_MAIN]])


def order_kb(direction: str):
    name = direction.split(" ", 1)[1]
    return kb([[BTN_V_MAKE, BTN_V_FILES], [BTN_V_CANCEL, BTN_V_NEW + name], [BTN_MAIN]])


CONFIRM_SVC_KB = InlineKeyboardMarkup(inline_keyboard=[
    [InlineKeyboardButton(text="✅ Buyurtmani tasdiqlash", callback_data="svc_ok")],
    [InlineKeyboardButton(text="❌ Bekor qilish", callback_data="svc_no")]])
CONFIRM_KB = InlineKeyboardMarkup(inline_keyboard=[[
    InlineKeyboardButton(text="✅ Tasdiqlash", callback_data="order_ok"),
    InlineKeyboardButton(text="❌ Bekor qilish", callback_data="order_no")]])
CONFIRM_SWAP_KB = InlineKeyboardMarkup(inline_keyboard=[
    [InlineKeyboardButton(text="✅ Tasdiqlash", callback_data="order_ok"),
     InlineKeyboardButton(text="🔀 Boshqa tartib", callback_data="swap_re")],
    [InlineKeyboardButton(text="❌ Bekor qilish", callback_data="order_no")]])
ACCEPTED_MSG = ("⏳ Buyurtmangiz qabul qilindi. Natija tayyor bo'lgach shu chatga yuboriladi. "
                "Kutish paytida botning boshqa bo'limlaridan foydalanishingiz mumkin.")
CONFIRM_TAIL = "Boshlash uchun quyidagi tasdiqlash tugmasini bosing. Xatolikda bepul urinish qaytariladi."

# ---------- Kanal ochish xizmatlari ----------
BASE = ("Sen TubeNova AI — YouTuberlar uchun yordamchisan. Foydalanuvchi qaysi tilda yozsa, shu tilda javob ber "
        "(odatda o'zbek tilida, lotin yozuvida). Foydalanuvchining xabarini diqqat bilan o'qi va aynan u "
        "so'ragan narsani qil. Aniq va amaliy yoz. Markdown belgilarini (**, #, ```) HECH QACHON ishlatma: "
        "faqat oddiy matn va emoji.")

ASK_TAIL = "\n\nJavoblarni bitta xabarda yozib yuboring."
SERVICES = {
    "logo": {"btn": "🎨 Logo", "design": True,
             "ask": "Logo uchun:\n1) Kanal nomi nima?\n2) Yo'nalishi qanday?\n"
                    "3) Qaysi rang va uslub yoqadi? (ixtiyoriy)" + ASK_TAIL},
    "banner": {"btn": "🖼 Banner", "design": True,
               "ask": "Banner uchun:\n1) Kanal nomi nima?\n2) Yo'nalishi qanday?\n"
                      "3) Bannerda qanday yozuv va rang bo'lsin? (ixtiyoriy)" + ASK_TAIL},
    "tags": {"btn": "🏷 Teglar",
             "ask": "Teglar uchun:\n1) Kanal nomi nima?\n2) Videolar qaysi mavzuda?\n"
                    "3) Teglar qaysi tilda bo'lsin?" + ASK_TAIL},
    "about": {"btn": "📝 Kanal tavsifi",
              "ask": "Kanal tavsifi uchun:\n1) Kanal nomi nima?\n2) Kanalda nimalar ko'rsatiladi?\n"
                     "3) Qaysi tilda va kimlar uchun yozamiz?" + ASK_TAIL},
}
SERVICE_BY_BTN = {s["btn"]: k for k, s in SERVICES.items()}
CHANNEL_KB = kb([[SERVICES["logo"]["btn"], SERVICES["banner"]["btn"]],
                 [SERVICES["tags"]["btn"], SERVICES["about"]["btn"]],
                 [BTN_ANALYSIS], [BTN_MAIN]])
CHANNEL_MSG = "Kanalingiz uchun nima kerak?\n\nLogo, banner, teglar, kanal tavsifi yoki YouTube tahlilini tanlang."

# Mavzularni AI to'g'ri tushunishi uchun qisqa izohlar
GLOSSARY = {
    "tiles hop": "Tiles Hop is a mobile rhythm/music arcade game: a ball hops from tile to tile to the beat of a "
                 "song. Channels post gameplay videos and character-themed music challenges, often as Shorts.",
    "coffin dance": "Coffin Dance is the viral meme of pallbearers dancing to the song 'Astronomia'; channels post "
                    "meme edits and beat-synced character videos.",
    "minecraft": "Minecraft is a sandbox building and survival game.",
    "roblox": "Roblox is an online platform with user-made games.",
    "free fire": "Free Fire is a mobile battle royale shooter game.",
    "pubg": "PUBG Mobile is a mobile battle royale shooter game.",
}


def glossary_for(text: str) -> str:
    t = text.lower()
    return " ".join(v for k, v in GLOSSARY.items() if k in t)


EXTRACT_SYSTEM = (
    "You read a short, informal message (Uzbek, Russian or English, often a numbered list) and extract fields. "
    "Return ONLY a JSON object, no other text: "
    '{"name": "...", "topic": "...", "language": "...", "audience": "..."}. '
    "name = the channel name exactly as written, without list numbering such as '1.'. "
    "topic = what the channel/videos are about, in English, using the user's own words (for example "
    "'Tiles Hop game videos'); never replace it with a different topic. "
    "language = the language the OUTPUT must be written in, as an English language name: 'ingliz tilida', "
    "'inglizcha', 'English' -> English; 'o'zbek', 'o'zbekcha' -> Uzbek; 'rus', 'ruscha', 'русский' -> Russian; "
    "two languages -> 'English and Uzbek'. If no language is given -> Uzbek. "
    "audience = target viewers if mentioned, otherwise an empty string.")

TAGS_SYSTEM = (
    "You write YouTube SEO tags. Output ONLY the tags: one single line, exactly 20 tags separated by commas with "
    "NO space after the commas (example: Name,Main topic,Main topic gameplay,music game,arcade game). "
    "Language of the tags: {language} (keep proper nouns and game names unchanged). "
    "Tag 1 = the exact channel name \"{name}\". Tag 2 = the main topic \"{topic}\". Tags 3-20 = short 2-4 word "
    "search phrases real viewers type, specific to this niche: gameplay, format, genre, difficulty, device, "
    "challenge, shorts, trends. Stay strictly on the user's topic; never invent a different game or topic. "
    "No hashtags, numbering, quotes, explanations or duplicates. Maximum 500 characters in total. {glossary}")

ABOUT_SYSTEM = (
    "You write a YouTube channel description ('About'). Write ONLY in {language}. 600-900 characters. Structure: "
    "a catchy first sentence with the main keywords; what the channel shows (strictly based on: {topic}); who it is "
    "for ({audience}); a short call to subscribe; 2-3 fitting emojis; the last line = 5-8 keywords separated by "
    "commas. Use the channel name \"{name}\". Do not invent facts (no upload schedule, numbers or claims) unless "
    "the user gave them. Plain text only, no markdown. {glossary}")

DESIGN_SYSTEM = (
    "You prepare briefs for logo/banner artwork. The user's message may be in Uzbek, Russian or English and is "
    "informal (often a numbered list). Return ONLY one JSON object, no other text: "
    '{"name": "<channel name exactly as written, without list numbering>", '
    '"text": "<exact text to print on the banner; default = the channel name; use other text only if the user '
    'explicitly asked for banner text>", "niche": "<channel niche in 3-8 English words>", '
    '"motif": "<1-2 vivid English phrases describing iconic artwork for this niche, for example \'a glowing '
    'perspective path of square tiles with a bouncing glossy ball\'; no text, no brand logos>", '
    '"colors": ["<English color names the user asked for, in order; if none asked, choose 2-3 that fit the niche>"]}. ')

LOGO_PROMPT = (
    "Premium esports YouTube channel logo badge, perfectly circular emblem on a pure black background, thick "
    "glowing {c1} ring border, symmetrical centered composition, generous empty margin around the circle. In the "
    "center: {motif}. Below the motif: one single bold sharp stylized capital letter \"{initial}\" in white with "
    "{c1} accent shadow. Color palette strictly {palette}. Glossy 3D vector illustration, neon rim light, high "
    "contrast, crisp edges. No other text, no watermark, no signature.")
BANNER_PROMPT = (
    "Ultra-wide cinematic YouTube channel banner artwork. Dark black-charcoal background with subtle {c1} smoky "
    "texture, flying glass shards and sparks. On the far left a glowing {motif}, on the far right a mirrored {c2} "
    "version. Large empty dark space in the center for a title. All important elements are arranged in a "
    "horizontal band in the vertical middle of the image; the top 30% and bottom 30% of the image are plain dark. "
    "Neon glow, sharp focus, palette strictly {palette}. No text, no letters, no watermark.")

QA_SYSTEM = BASE + (" Faqat YouTube, kontent yaratish, video tahrirlash va kanal o'stirish "
                    "mavzularida javob ber. Boshqa mavzuda xushmuomalalik bilan YouTube'ga qaytar.")

WELCOME = ("Assalomu alaykum! TubeNova AI yordamchisiga xush kelibsiz.\n\n"
           "Sizga qanday yordam kerak? Quyidagi tugmalardan tanlang.")
SERVICES_MSG = ("🛍 Xizmatlar\n\nKerakli xizmatni tanlang:\n\n📺 Kanal ochish\n🎬 Video tayyorlash\n"
                "🔀 Video joyini almashtirish")
VIDEO_MSG = ("🎬 Video tayyorlash\n\nYo'nalishni tanlang: 🎮 Tiles Hop, 🕺 Coffin Dance, 👽 Alien Dance "
             "👾 Tiles Hop EXE, 🏆 GOLDEN COVER 🎤 Say The Word yoki 👀 Wrong Eyes.")
LIMIT_MSG = ("⏳ Bugungi limit tugadi. Limit har kuni 00:00 da (Toshkent vaqti) yangilanadi.\n"
             "Do'stlaringizni taklif qilib bonus urinishlar oling: «" + BTN_REF + "».")
TOS_TEXT = ("📜 Foydalanish shartlari\n\n"
            "• Bot bepul, kunlik limitlar bilan ishlaydi.\n"
            "• AI javoblari yo'l-yo'riq sifatida beriladi, natija kafolatlanmaydi.\n"
            "• Qonunga zid yoki boshqalarning huquqini buzadigan so'rovlar qabul qilinmaydi.\n"
            "• Yuborgan rasm va matnlaringiz faqat xizmat ko'rsatish uchun ishlatiladi.")
COFFIN_INTRO = (
    "🕺 Coffin Dance\n\n"
    "Avval musiqa yoki musiqasi bor video, keyin montaj uchun boshqa video yuboring. Birinchi fayldan "
    "faqat ovoz, ikkinchi videodan tasvir olinadi. Qisqa harakatlar musiqa zarblarida takrorlanadi, "
    "oldinga–orqaga aylantiriladi va yaqinlashtiriladi. Har to'rt zarbdan keyin boshqa parcha olinadi. "
    "Videoning asl ovozi o'rniga siz yuborgan musiqa ishlatiladi. Tayyor video maksimum 1 daqiqa 7 soniya. "
    "Musiqa qisqaroq bo'lsa, rolik ham qisqaroq bo'ladi. Qisqa videodan parchalar takrorlanishi mumkin.\n\n")


# ---------- Matnni tozalash (** va boshqa markdown belgilarini olib tashlaydi) ----------
def clean(t: str) -> str:
    t = re.sub(r"\*\*(.+?)\*\*", r"\1", t, flags=re.S)
    t = re.sub(r"__(.+?)__", r"\1", t, flags=re.S)
    t = t.replace("**", "").replace("__", "")
    t = re.sub(r"`+", "", t)
    t = re.sub(r"(?m)^\s{0,3}#{1,6}\s*", "", t)
    t = re.sub(r"(?m)^(\s*)\*\s+", r"\1• ", t)
    t = re.sub(r"(?<![\w*])\*([^\s*][^*\n]*?)\*(?![\w*])", r"\1", t)
    return t.strip()


def parse_json(text: str) -> dict:
    mt = re.search(r"\{.*\}", text, re.S)
    try:
        return json.loads(mt.group(0)) if mt else {}
    except Exception:
        return {}


def first_name(text: str) -> str:
    """Birinchi qatordan kanal nomini oladi (raqamlash belgilarisiz)."""
    line = text.strip().splitlines()[0] if text.strip() else "Kanal"
    return re.sub(r"^\s*\d+\s*[.)\-:]\s*", "", line).strip()[:40] or "Kanal"


# ---------- Ma'lumotlar bazasi ----------
def today() -> str:
    return datetime.now(TZ).strftime("%Y-%m-%d")


async def init_db():
    async with aiosqlite.connect(DB) as db:
        await db.execute("""CREATE TABLE IF NOT EXISTS users(
            id INTEGER PRIMARY KEY, name TEXT, ref_code TEXT UNIQUE,
            referred_by INTEGER, bonus INTEGER DEFAULT 0,
            day TEXT, q_used INTEGER DEFAULT 0, s_used INTEGER DEFAULT 0)""")
        await db.commit()


async def register(uid: int, name: str, ref_code: str | None = None):
    """Yangi foydalanuvchini yozadi. (yangimi, taklif_qilgan_id) qaytaradi."""
    async with aiosqlite.connect(DB) as db:
        cur = await db.execute("SELECT 1 FROM users WHERE id=?", (uid,))
        if await cur.fetchone():
            return False, None
        referrer = None
        if ref_code:
            cur = await db.execute("SELECT id FROM users WHERE ref_code=?", (ref_code,))
            row = await cur.fetchone()
            if row and row[0] != uid:
                referrer = row[0]
        await db.execute(
            "INSERT INTO users(id,name,ref_code,referred_by,day) VALUES(?,?,?,?,?)",
            (uid, name, secrets.token_urlsafe(6), referrer, today()))
        if referrer:
            await db.execute("UPDATE users SET bonus=bonus+? WHERE id=?", (REF_BONUS, referrer))
        await db.commit()
        return True, referrer


async def get_user(uid: int):
    async with aiosqlite.connect(DB) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute("SELECT * FROM users WHERE id=?", (uid,))
        row = await cur.fetchone()
        if row is None:
            return None
        if row["day"] != today():  # yangi kun — limitlar yangilanadi
            await db.execute("UPDATE users SET day=?, q_used=0, s_used=0 WHERE id=?", (today(), uid))
            await db.commit()
            cur = await db.execute("SELECT * FROM users WHERE id=?", (uid,))
            row = await cur.fetchone()
        return dict(row)


async def consume(uid: int, kind: str):
    """Bitta urinishni sarflaydi. Manba: 'u' (cheksiz) | 'q' | 's' | 'b' | None."""
    if uid in UNLIMITED:
        return "u"
    u = await get_user(uid)
    async with aiosqlite.connect(DB) as db:
        if kind == "q":
            if u["q_used"] >= DAILY_Q:
                return None
            await db.execute("UPDATE users SET q_used=q_used+1 WHERE id=?", (uid,))
            src = "q"
        elif u["s_used"] < DAILY_S:
            await db.execute("UPDATE users SET s_used=s_used+1 WHERE id=?", (uid,))
            src = "s"
        elif u["bonus"] > 0:
            await db.execute("UPDATE users SET bonus=bonus-1 WHERE id=?", (uid,))
            src = "b"
        else:
            return None
        await db.commit()
        return src


async def refund(uid: int, src: str):
    if src == "u":
        return
    col = {"q": "q_used", "s": "s_used"}.get(src)
    async with aiosqlite.connect(DB) as db:
        if col:
            await db.execute(f"UPDATE users SET {col}=MAX({col}-1,0) WHERE id=?", (uid,))
        else:
            await db.execute("UPDATE users SET bonus=bonus+1 WHERE id=?", (uid,))
        await db.commit()




# ---------- AI ----------
async def ask_ai(system: str, prompt: str, max_tokens: int = 2000) -> str:
    kw = {}
    if MODEL.startswith("openai/gpt-oss"):
        kw["extra_body"] = {"reasoning_effort": "low"}  # tez va bo'sh javob chiqmasligi uchun
    r = await ai.chat.completions.create(
        model=MODEL, max_tokens=max_tokens,
        messages=[{"role": "system", "content": system}, {"role": "user", "content": prompt}], **kw)
    return (r.choices[0].message.content or "").strip()


async def send_long(bot: Bot, chat_id: int, text: str, **kw):
    for i in range(0, len(text), 4000):
        await bot.send_message(chat_id, text[i:i + 4000], **kw)


async def answer_with_ai(m: Message, kind: str, system: str, prompt: str, max_tokens=2000):
    src = await consume(m.from_user.id, kind)
    if not src:
        await m.answer(LIMIT_MSG)
        return False
    await m.bot.send_chat_action(m.chat.id, "typing")
    try:
        text = await ask_ai(system, prompt[:3000], max_tokens)
    except Exception:
        log.exception("AI xatosi")
        await refund(m.from_user.id, src)
        await m.answer("⚠️ Hozir xatolik yuz berdi. Limitingiz qaytarildi, birozdan keyin urinib ko'ring.")
        return False
    await send_long(m.bot, m.chat.id, clean(text) or "Javob bo'sh chiqdi, iltimos qayta yozing.")
    return True


# ---------- Kanal xizmatlari (fon vazifalari) ----------
async def design_job(bot: Bot, chat_id: int, uid: int, src: str, kind: str, text: str):
    """Logo yoki banner RASMini yaratadi: AI rasm, bo'lmasa nom va ranglar asosida zaxira dizayn."""
    await bot.send_chat_action(chat_id, "upload_photo")
    brief = {}
    try:
        brief = parse_json(await ask_ai(DESIGN_SYSTEM + glossary_for(text), text[:1500], 1500))
    except Exception:
        log.exception("Dizayn tavsifi xatosi")
    name = re.sub(r"^\s*\d+\s*[.)\-:]\s*", "", str(brief.get("name") or first_name(text))).strip()[:40] or "Kanal"
    title = (str(brief.get("text") or name).strip()[:40] or name) if kind == "banner" else name
    hexes = media.colors_from_text(text) or media.names_to_hex(brief.get("colors"))
    hexes = hexes[:3] or ["#e53935", "#ffffff"]
    palette = media.make_palette(hexes, [])
    names = media.color_names(hexes)
    motif = str(brief.get("motif") or "").strip() or f"bold glowing emblem symbolizing {brief.get('niche') or name}"
    fields = dict(c1=names[0], c2=names[1] if len(names) > 1 else names[0], palette=", ".join(names),
                  motif=motif, initial=media._initial(name))
    fallback = False
    try:
        if kind == "logo":
            img = await media.fetch_image(LOGO_PROMPT.format(**fields), 1024, 1024, wm=40)
        else:
            art = await media.fetch_image(BANNER_PROMPT.format(**fields), 1792, 1024)
            img = await asyncio.to_thread(media.compose_banner, art, title, palette)
    except Exception:
        log.exception("AI rasm xizmati ishlamadi, zaxira dizayn ishlatiladi")
        fallback = True
        draw = media.draw_logo if kind == "logo" else media.draw_banner
        img = await asyncio.to_thread(draw, name if kind == "logo" else title, palette)
    try:
        if kind == "logo":
            data, fname = media.to_bytes(img, "PNG"), "logo.png"
        else:
            data, fname = media.to_bytes(img, "JPEG"), "banner.jpg"
        cap = "🎨 Logo tayyor!" if kind == "logo" else "🖼 Banner tayyor (2560×1440)!"
        if fallback:
            cap += "\n(AI rasm xizmati band bo'lgani uchun nom va ranglaringiz asosida dizayn qilindi)"
        await bot.send_photo(chat_id, BufferedInputFile(data, fname), caption=cap)
        await bot.send_document(chat_id, BufferedInputFile(data, fname),
                                caption="Asl fayl (sifati buzilmagan). Yoqmasa, xizmatni qayta tanlab, "
                                        "boshqacha tasvirlab yuboring.")
    except Exception:
        log.exception("Rasmni yuborishda xato")
        await refund(uid, src)
        await bot.send_message(chat_id, "⚠️ Rasmni yuborib bo'lmadi. Limitingiz qaytarildi, qayta urinib ko'ring.")


def fix_tags(t: str) -> str:
    t = clean(t).replace("\n", ",").replace("#", "")
    t = re.sub(r"(^|,)\s*\d+[.)]\s*", r"\1", t)
    t = re.sub(r"\s*,\s*", ",", t)
    return re.sub(r",{2,}", ",", t).strip(", ")


async def text_job(bot: Bot, chat_id: int, uid: int, src: str, key: str, text: str):
    """Teglar yoki kanal tavsifi: avval xabar tushuniladi (nom, mavzu, til), keyin shu tilda yoziladi."""
    await bot.send_chat_action(chat_id, "typing")
    try:
        f = parse_json(await ask_ai(EXTRACT_SYSTEM, text[:1500], 800))
        name = re.sub(r"^\s*\d+\s*[.)\-:]\s*", "", str(f.get("name") or first_name(text))).strip() or "Kanal"
        topic = str(f.get("topic") or text).strip()
        language = str(f.get("language") or "Uzbek").strip()
        audience = str(f.get("audience") or "").strip() or "general viewers"
        system = (TAGS_SYSTEM if key == "tags" else ABOUT_SYSTEM).format(
            language=language, name=name, topic=topic, audience=audience, glossary=glossary_for(text + " " + topic))
        out = await ask_ai(system, f"Channel name: {name}\nTopic: {topic}\nOutput language: {language}\n"
                                   f"Audience: {audience}\nUser's original message:\n{text}", 2000)
        out = fix_tags(out) if key == "tags" else clean(out)
        if not out:
            raise ValueError("bo'sh javob")
    except Exception:
        log.exception("Matn xizmati xatosi")
        await refund(uid, src)
        await bot.send_message(chat_id, "⚠️ Hozir xatolik yuz berdi. Limitingiz qaytarildi, birozdan keyin urinib ko'ring.")
        return
    await send_long(bot, chat_id, out)


async def analysis_job(bot: Bot, chat_id: int, uid: int, src: str, d: dict):
    await bot.send_chat_action(chat_id, "typing")
    try:
        images = []
        for fid in d["shots"]:
            buf = io.BytesIO()
            await bot.download(fid, destination=buf)
            images.append(buf.getvalue())
        text, sources = await analysis.analyze(ai, lambda s, p: ask_ai(s, p, 4000), d["link"], d["notes"], images)
        text = analysis.with_sources(clean(text), sources)
    except Exception:
        log.exception("Tahlil xatosi")
        await refund(uid, src)
        await bot.send_message(chat_id, "⚠️ Tahlil qilib bo'lmadi. Limitingiz qaytarildi, birozdan keyin urinib ko'ring.")
        return
    await send_long(bot, chat_id, text or "Javob bo'sh chiqdi, qayta urinib ko'ring.")


# ---------- Holatlar ----------
class Flow(StatesGroup):
    qa = State()
    service = State()
    video = State()
    analysis = State()


# ---------- Video buyurtma: umumiy yordamchilar ----------
async def fetch(bot: Bot, fid: str, path: str, timeout: int = 600) -> str:
    """Faylni yuklab oladi. MTProto yoqilgan bo'lsa, 20 MB dan katta fayllar ham olinadi."""
    if tg_client is not None:
        try:
            got = await tg_client.download_media(fid, file_name=path)
            if got and os.path.exists(str(got)):
                return str(got)
        except Exception:
            log.exception("MTProto yuklab olish xatosi, oddiy usulga o'tilmoqda")
    await bot.download(fid, destination=path, timeout=timeout)
    return path


def new_order(prev=None) -> list:
    while True:
        o = random.sample(range(4), 4)
        if o != [0, 1, 2, 3] and o != prev:
            return o


def fmt_order(o: list) -> str:
    return " → ".join(str(i + 1) for i in o)


def parse_order(text: str):
    nums = re.findall(r"[1-4]", text)
    if len(nums) == 4 and len(set(nums)) == 4 and len(re.sub(r"[\s,.\-→>]", "", text)) == 4:
        return [int(x) - 1 for x in nums]
    return None


def swap_prompt(d: dict) -> str:
    if not d["video"]:
        return f"🎞 Tiles Hop videosini yuboring. Video maksimum {max_mb()} MB."
    return ("✅ Video tayyor. Yangi tartibni 4 ta raqam bilan yozishingiz mumkin (masalan: 3 1 4 2), "
            "yozmasangiz bot o'zi aralashtiradi. Tayyor bo'lsa, «Videoni tayyorlash» tugmasini bosing.")


def media_kind(m: Message):
    if m.photo:
        return "img"
    if m.video:
        return "vid"
    if m.audio or m.voice:
        return "aud"
    d = m.document
    if d and d.mime_type:
        if d.mime_type.startswith("image/"):
            return "img"
        if d.mime_type.startswith("video/"):
            return "vid"
        if d.mime_type.startswith("audio/"):
            return "aud"
    return None


def tiles_prompt(d: dict) -> str:
    imgs, vids = d["imgs"], d["vids"]
    if len(imgs) < N:
        return (f"🖼 {len(imgs) + 1}-qahramon rasmini yuboring. Qahramon to'liq ko'rinsin. "
                "Fon avtomatik olib tashlanadi, PNG o'lchami moslanadi.")
    if len(vids) < N:
        return f"🎞 {len(vids) + 1}-coffin videoni yuboring."
    return "✅ Hammasi tayyor. «Videoni tayyorlash» tugmasini bosing."


def coffin_prompt(d: dict) -> str:
    if not d["music"]:
        return (f"🎵 Musiqani yoki musiqasi bor videoni yuboring. Fayl maksimum {max_mb()} MB. "
                "Video yuborsangiz, uning ovozi musiqa sifatida ajratib olinadi.")
    if not d["video"]:
        return f"🎬 Endi montaj uchun videoni yuboring. Video maksimum {max_mb()} MB."
    return "✅ Hammasi tayyor. «Videoni tayyorlash» tugmasini bosing."


def next_prompt(d: dict) -> str:
    return {"tiles": tiles_prompt, "coffin": coffin_prompt, "swap": swap_prompt}[d["kind"]](d)


def progress_of(d: dict):
    """(qabul qilingan, jami, yetishmayotganlar)"""
    if d["kind"] == "tiles":
        imgs, vids = d["imgs"], d["vids"]
        miss = ([f"{i}-rasm" for i in range(len(imgs) + 1, N + 1)] +
                [f"{i}-video" for i in range(len(vids) + 1, N + 1)])
        return len(imgs) + len(vids), 2 * N, miss
    if d["kind"] == "swap":
        return (1, 1, []) if d["video"] else (0, 1, ["video"])
    miss = (["musiqa"] if not d["music"] else []) + (["video"] if not d["video"] else [])
    return 2 - len(miss), 2, miss


def take_file(d: dict, kind, item: dict) -> str:
    """Yuborilgan faylni buyurtmaga qo'shadi va javob matnini qaytaradi."""
    if d["kind"] == "tiles":
        imgs, vids = d["imgs"], d["vids"]
        if kind is None:
            pre = "Faqat rasm yoki video yuboring.\n\n"
        elif kind == "img" and len(imgs) < N:
            imgs.append(item)
            pre = f"✅ {len(imgs)}-rasm qabul qilindi.\n\n"
        elif kind == "img":
            pre = "Rasmlar to'liq.\n\n"
        elif kind != "vid":
            pre = "Bu yerda faqat rasm va video kerak.\n\n"
        elif len(imgs) < N:
            pre = "Avval rasmlarni yuboring.\n\n"
        elif len(vids) < N:
            vids.append(item)
            pre = f"✅ {len(vids)}-video qabul qilindi.\n\n"
        else:
            pre = "Videolar to'liq.\n\n"
        return pre + tiles_prompt(d)
    if d["kind"] == "swap":
        if kind == "vid":
            d["video"] = item
            pre = "✅ Video qabul qilindi.\n\n"
        else:
            pre = "Faqat video yuboring.\n\n"
        return pre + swap_prompt(d)
    if not d["music"]:
        if kind in ("aud", "vid"):
            d["music"] = item
            pre = "✅ Musiqa qabul qilindi.\n\n"
        else:
            pre = "Avval musiqa yoki musiqasi bor video yuboring.\n\n"
    elif not d["video"]:
        if kind == "vid":
            d["video"] = item
            pre = "✅ Video qabul qilindi.\n\n"
        else:
            pre = "Montaj uchun video yuboring.\n\n"
    else:
        pre = "Fayllar to'liq.\n\n"
    return pre + coffin_prompt(d)


async def start_order(m: Message, state: FSMContext, direction: str):
    name = direction.split(" ", 1)[1]
    if name not in READY:
        await m.answer(f"🚧 {name} yo'nalishi tez orada qo'shiladi. "
                       "Hozircha 🎮 Tiles Hop va 🕺 Coffin Dance mavjud.", reply_markup=VIDEO_KB)
        return
    if name == "Video joyini almashtirish":
        d = {"kind": "swap", "direction": direction, "video": None, "order": None}
        text = ("🔀 Video joyini almashtirish\n\nTiles Hop videosini yuboring (4 ta panelli tayyor video). "
                "Bot personajlar joyini almashtiradi, video esa har doimgidek chap tomondagi 1-o'rindan "
                "boshlanadi. Subscribe tugmasi kabi boshqa qismlar o'z joyida qoladi.\n\n"
                "Tayyorlash: taxminan 3–8 daqiqa. Navbat kutish vaqti alohida ko'rsatiladi.\n\n" + swap_prompt(d))
    elif name == "Tiles Hop":
        d = {"kind": "tiles", "direction": direction, "imgs": [], "vids": []}
        text = (f"{direction}\n\nTayyorlash: taxminan 2–4 daqiqa. Navbat kutish vaqti alohida ko'rsatiladi.\n\n"
                f"Avval {N} ta rasm, keyin shu tartibdagi {N} ta coffin video yuboring.\n\n"
                + tiles_prompt({"imgs": [], "vids": []}))
    else:
        d = {"kind": "coffin", "direction": direction, "music": None, "video": None}
        text = COFFIN_INTRO + coffin_prompt(d)
    await state.set_state(Flow.video)
    await state.set_data(d)
    await m.answer(text, reply_markup=order_kb(direction))


def confirm_text(d: dict, uid: int) -> str:
    title = d["direction"].split(" ", 1)[1]
    cost = "" if uid in UNLIMITED else "\n\nTasdiqlansa, 1 ta xizmat urinishi sarflanadi."
    if d["kind"] == "swap":
        o = d["order"]
        return (f"📋 Buyurtmani tasdiqlang\n\n🔀 Video joyini almashtirish\n🎞 Video: 1/1\n"
                f"🔀 Yangi tartib: {fmt_order(o)}\n\n"
                f"1-o'ringa hozirgi {o[0] + 1}-panel qo'yiladi va video undan boshlanadi. Subscribe tugmasi "
                "kabi qismlar o'z joyida qoladi. Boshqa tartib kerak bo'lsa, «Boshqa tartib» tugmasini bosing "
                "yoki 4 ta raqam yozing (masalan 3 1 4 2)." + cost)
    if d["kind"] == "tiles":
        body = (f"🖼 Rasmlar: {len(d['imgs'])}/{N}\n🎞 Videolar: {len(d['vids'])}/{N}\n\n"
                "Video shunday tayyorlanadi: avval 1-video o'z ovozi bilan o'ynaydi, qolgan 3 tasi "
                "yuborgan rasmingiz bilan to'xtab turadi. Keyin 2-, 3- va 4-video ketma-ket. "
                "Oxirida prevyu ham yuboriladi.")
    else:
        body = ("🎵 Musiqa: 1/1\n🎬 Video: 1/1\n\nVideo musiqa zarblariga mos montaj qilinadi "
                "(maksimum 1 daqiqa 7 soniya).")
    return f"📋 Buyurtmani tasdiqlang\n\n🎬 {title}\n{body}{cost}"


# ---------- Video tayyorlash ishlari ----------
async def tiles_job(bot: Bot, chat_id: int, d: dict, step):
    title = d["direction"].split(" ", 1)[1]
    with tempfile.TemporaryDirectory() as tmp:
        vpaths, ipaths = [], []
        total_files = len(d["vids"]) + len(d["imgs"])
        done = 0
        for i, it in enumerate(d["vids"]):
            done += 1
            await step(f"📥 Fayllar yuklab olinmoqda ({done}/{total_files})...")
            vpaths.append(await fetch(bot, it["fid"], os.path.join(tmp, f"v{i}.mp4")))
        for i, it in enumerate(d["imgs"]):
            done += 1
            await step(f"📥 Fayllar yuklab olinmoqda ({done}/{total_files})...")
            ipaths.append(await fetch(bot, it["fid"], os.path.join(tmp, f"i{i}.jpg"), timeout=120))
        await step("🖼 Rasm panellari tayyorlanmoqda...")
        panels = await asyncio.to_thread(
            lambda: [media.make_panel(p, i, render.PANEL, render.OUT_H, os.path.join(tmp, f"panel{i}.png"))
                     for i, p in enumerate(ipaths)])
        out = os.path.join(tmp, "video.mp4")
        total = await render.build(vpaths, panels, out, tmp, progress=step)
        await step("🖼 Prevyu tayyorlanmoqda...")
        preview = await asyncio.to_thread(media.make_preview, ipaths, title)
        await step("📤 Yuborilmoqda (video hajmiga qarab bir necha daqiqa ketishi mumkin)...")
        await bot.send_video(
            chat_id, FSInputFile(out), caption=f"🎬 {title} — video tayyor!",
            width=render.PANEL * 4, height=render.OUT_H, duration=int(total),
            thumbnail=BufferedInputFile(media.thumb_bytes(preview), "thumb.jpg"),
            supports_streaming=True, request_timeout=900)
        await bot.send_photo(chat_id, BufferedInputFile(preview, "prevyu.jpg"), caption="🖼 Prevyu")
        await bot.send_document(chat_id, BufferedInputFile(preview, "prevyu.jpg"),
                                caption="Prevyu — asl fayl (1280×720).")


async def coffin_job(bot: Bot, chat_id: int, d: dict, step):
    with tempfile.TemporaryDirectory() as tmp:
        await step("📥 Fayllar yuklab olinmoqda (1/2)...")
        mp = await fetch(bot, d["music"]["fid"], os.path.join(tmp, "music.bin"))
        await step("📥 Fayllar yuklab olinmoqda (2/2)...")
        vp = await fetch(bot, d["video"]["fid"], os.path.join(tmp, "src.mp4"))
        out = os.path.join(tmp, "coffin.mp4")
        total = await coffin.build(mp, vp, out, tmp, progress=step)
        await step("📤 Yuborilmoqda (video hajmiga qarab bir necha daqiqa ketishi mumkin)...")
        await bot.send_video(chat_id, FSInputFile(out), caption="🕺 Coffin Dance — video tayyor!",
                             width=coffin.VW, height=coffin.VH, duration=int(total),
                             supports_streaming=True, request_timeout=900)


async def swap_job(bot: Bot, chat_id: int, d: dict, step):
    with tempfile.TemporaryDirectory() as tmp:
        await step("📥 Video yuklab olinmoqda...")
        vp = await fetch(bot, d["video"]["fid"], os.path.join(tmp, "src.mp4"))
        out = os.path.join(tmp, "swapped.mp4")
        res = await swapper.build(vp, d["order"], out, tmp, progress=step)
        preview = None
        if len(res["frames"]) == 4:
            await step("🖼 Prevyu tayyorlanmoqda...")
            try:
                heroes = [media.oval_cutout(Image.open(p)) for p in res["frames"]]
                preview = await asyncio.to_thread(media.make_preview_imgs, heroes, "Tiles Hop")
            except Exception:
                log.exception("Prevyu yasalmadi")
        await step("📤 Yuborilmoqda (video hajmiga qarab bir necha daqiqa ketishi mumkin)...")
        thumb = BufferedInputFile(media.thumb_bytes(preview), "thumb.jpg") if preview else None
        await bot.send_video(chat_id, FSInputFile(out),
                             caption=f"🔀 Video tayyor! Yangi tartib: {fmt_order(d['order'])}",
                             duration=int(res["total"]), thumbnail=thumb, supports_streaming=True,
                             request_timeout=900)
        if preview:
            await bot.send_photo(chat_id, BufferedInputFile(preview, "prevyu.jpg"), caption="🖼 Prevyu")
            await bot.send_document(chat_id, BufferedInputFile(preview, "prevyu.jpg"),
                                    caption="Prevyu — asl fayl (1280×720).")
        if not res["ordered"]:
            await bot.send_message(chat_id, "ℹ️ Videoda panellar ketma-ket o'ynamagani uchun faqat panel joylari "
                                            "almashtirildi, ovoz va vaqt o'zgarmadi.")


async def run_order(bot: Bot, chat_id: int, uid: int, src: str, d: dict):
    """Tasdiqlangan buyurtma (fonda): jarayon xabarlari bilan tayyorlab, natijani yuboradi."""
    RENDERING.add(uid)
    status = await bot.send_message(
        chat_id, "⏳ Navbatda turibsiz, iltimos kuting..." if RENDER_SEM.locked() else "⏳ Boshlanmoqda...")

    async def step(text: str):
        try:
            await status.edit_text("⏳ Video tayyorlanmoqda\n\n" + text)
        except Exception:
            pass

    try:
        async with RENDER_SEM:
            await {"tiles": tiles_job, "coffin": coffin_job, "swap": swap_job}[d["kind"]](bot, chat_id, d, step)
        await bot.send_message(chat_id, "✅ Buyurtmangiz tayyor!")
    except ValueError as e:
        await refund(uid, src)
        await bot.send_message(chat_id, f"⚠️ {e} Limitingiz qaytarildi.")
    except Exception:
        log.exception("Video tayyorlashda xato")
        await refund(uid, src)
        await bot.send_message(chat_id, "⚠️ Video tayyorlab bo'lmadi. Limitingiz qaytarildi. Fayllarni "
                                        f"tekshirib, qayta urinib ko'ring (har bir fayl {max_mb()} MB dan oshmasin).")
    finally:
        RENDERING.discard(uid)
        try:
            await status.delete()
        except Exception:
            pass


# ---------- YouTube tahlil ----------
YT_RE = re.compile(r"(?:https?://)?(?:www\.|m\.)?(?:youtube\.com|youtu\.be)/\S+", re.I)


def analysis_intro() -> str:
    if analysis.vision_enabled():
        return ("1-qadam. YouTube kanalingiz havolasi qanday?\n\nMasalan: https://www.youtube.com/@holitech\n"
                "Havolani yuboring, keyin Studio skrinshotlarini so'rayman.\n\n"
                "Rasmlar AI orqali tahlil qilinadi; yuborgan ma'lumotlaringiz faqat tahlil uchun ishlatiladi.")
    return ("1-qadam. YouTube kanalingiz havolasi qanday?\n\nMasalan: https://www.youtube.com/@holitech\n"
            "Havolani yuboring, keyin Studio raqamlarini so'rayman.")


def analysis_prompt(d: dict) -> str:
    if not d["link"]:
        return "Avval kanal havolasini yuboring."
    if d["notes"] or d["shots"]:
        return "Yana ma'lumot qo'shishingiz mumkin. Tayyor bo'lsa, «Tahlil qilish»ni bosing."
    if analysis.vision_enabled():
        return ("2-qadam. Studio'dan skrinshotlarni yuboring:\n1) Umumiy ko'rinish\n"
                "2) Kontent: CTR va tomosha davomiyligi\n3) Auditoriya: tomoshabinlar faol vaqti\n"
                "4) Videolar ro'yxati: yuklangan sanalar\n5) Ko'rishlar: oldingi davr bilan taqqoslash\n\n"
                "Sana oralig'i ko'rinsin. Turli bo'limlardan tiniq rasmlar foydaliroq.\n"
                f"Ko'pi bilan {MAX_SHOTS} ta rasm, har biri 8 MB gacha; bitta rasm bilan ham boshlash mumkin.\n"
                "Auditoriya mamlakati va vaqt mintaqasini ham yozishingiz mumkin.\n\n"
                "Tugatgach «Tahlil qilish»ni bosing.")
    return ("2-qadam. Studio raqamlarini matn qilib yozing: obunachilar, ko'rishlar, CTR, o'rtacha "
            "ko'rish davomiyligi, auditoriya mamlakati va oxirgi 3–5 ta video nomi.\n\n"
            "Tugatgach «Tahlil qilish»ni bosing.")


async def start_analysis(m: Message, state: FSMContext):
    await state.set_state(Flow.analysis)
    await state.set_data({"kind": "analysis", "link": None, "notes": [], "shots": []})
    await m.answer(analysis_intro(), reply_markup=ANALYSIS_KB)


# ---------- Menyu va tugmalar (holatga bog'liq xabarlardan OLDIN ro'yxatdan o'tadi) ----------
@router.message.outer_middleware()
async def ensure_user(handler, event, data):
    if event.from_user and not (event.text or "").startswith("/start"):
        if await get_user(event.from_user.id) is None:
            await register(event.from_user.id, event.from_user.full_name)
    return await handler(event, data)


@router.message(CommandStart())
async def start(m: Message, command: CommandObject, state: FSMContext):
    await state.clear()
    ref = command.args[4:] if command.args and command.args.startswith("ref_") else None
    _, referrer = await register(m.from_user.id, m.from_user.full_name, ref)
    if referrer:
        try:
            await m.bot.send_message(
                referrer, f"🎉 Havolangiz orqali yangi do'st qo'shildi! +{REF_BONUS} bonus urinish.")
        except Exception:
            pass
    await m.answer(WELCOME, reply_markup=MAIN_KB)


@router.message(F.text == BTN_MAIN)
async def to_main(m: Message, state: FSMContext):
    await state.clear()
    await m.answer(WELCOME, reply_markup=MAIN_KB)


@router.message(F.text == BTN_SERVICES)
async def services(m: Message, state: FSMContext):
    await state.clear()
    await m.answer(SERVICES_MSG, reply_markup=SERVICES_KB)


@router.message(F.text == BTN_LIMITS)
async def limits(m: Message):
    if m.from_user.id in UNLIMITED:
        await m.answer("🎁 Bepul imkoniyatlarim\n\n♾ Sizda cheklov yo'q.")
        return
    u = await get_user(m.from_user.id)
    await m.answer(
        "🎁 Bepul imkoniyatlarim\n\n"
        f"💬 Savol-Javob: bugun {DAILY_Q - u['q_used']}/{DAILY_Q} ta bepul\n"
        f"🛠 Xizmatlar: bugun {max(DAILY_S - u['s_used'], 0)}/{DAILY_S} ta bepul\n"
        f"⭐ Bonus urinishlar: {u['bonus']}\n\n"
        "Limit Toshkent vaqti bilan 00:00 da yangilanadi.")


@router.message(F.text == BTN_REF)
async def referral(m: Message):
    u = await get_user(m.from_user.id)
    link = f"https://t.me/{BOT_USERNAME}?start=ref_{u['ref_code']}"
    await m.answer(
        "👥 Do'stlarni taklif qiling\n\n"
        f"Sizning havolangiz:\n{link}\n\n"
        f"Havola orqali kirgan har bir yangi do'st uchun +{REF_BONUS} bonus urinish olasiz. "
        "Bonus faqat xizmatlarga sarflanadi. Har bir yangi Telegram hisob uchun faqat bir marta.")


@router.message(F.text == BTN_TOS)
async def tos(m: Message):
    await m.answer(TOS_TEXT)


@router.message(F.text == BTN_HELP)
async def help_(m: Message):
    markup = None
    if ADMIN_USERNAME:
        markup = InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="🆘 Yordam olish", url=f"https://t.me/{ADMIN_USERNAME}")]])
    await m.answer("✉️ Adminga yozish\n\nAdmin bilan bog'lanish uchun quyidagi tugmani bosing.\n\n"
                   "❗ Diqqat! Admin murojaatingizni 24 soat ichida ko'rib chiqib, javob beradi.",
                   reply_markup=markup)


@router.message(F.text == BTN_QA)
async def qa_start(m: Message, state: FSMContext):
    await state.set_state(Flow.qa)
    await m.answer(f"YouTube bo'yicha qanday savolingiz bor?\n\nKuniga {DAILY_Q} ta savol bepul. "
                   "Savolingizni qisqa va aniq yozing.", reply_markup=kb([[BTN_MAIN]]))


@router.message(F.text == BTN_CHANNEL)
async def channel_menu(m: Message, state: FSMContext):
    await state.clear()
    await m.answer(CHANNEL_MSG, reply_markup=CHANNEL_KB)


@router.message(F.text.in_(SERVICE_BY_BTN))
async def service_pick(m: Message, state: FSMContext):
    key = SERVICE_BY_BTN[m.text]
    await state.set_state(Flow.service)
    await state.set_data({"key": key, "text": None})
    await m.answer(SERVICES[key]["ask"], reply_markup=CHANNEL_KB)


# --- YouTube tahlil tugmalari
@router.message(F.text == BTN_ANALYSIS)
async def analysis_open(m: Message, state: FSMContext):
    await start_analysis(m, state)


@router.message(F.text == BTN_A_NEW)
async def analysis_new(m: Message, state: FSMContext):
    await start_analysis(m, state)


@router.message(F.text == BTN_A_CANCEL)
async def analysis_cancel(m: Message, state: FSMContext):
    await state.clear()
    await m.answer(CHANNEL_MSG, reply_markup=CHANNEL_KB)


@router.message(F.text == BTN_A_RUN)
async def analysis_run(m: Message, state: FSMContext):
    d = await state.get_data()
    if d.get("kind") != "analysis":
        await start_analysis(m, state)
        return
    miss = (["kanal havolasi"] if not d["link"] else []) + (
        ["Studio ma'lumotlari"] if not (d["notes"] or d["shots"]) else [])
    if miss:
        await m.answer("Hali yetishmayapti: " + ", ".join(miss) + ".\n\n" + analysis_prompt(d),
                       reply_markup=ANALYSIS_KB)
        return
    await m.answer(f"📊 YouTube tahlil\n\n{d['link']}\nSkrinshotlar: {len(d['shots'])}\n\n{CONFIRM_TAIL}",
                   reply_markup=CONFIRM_SVC_KB)


# --- Video tugmalari
@router.message(F.text == BTN_VIDEO)
async def video_menu(m: Message, state: FSMContext):
    await state.clear()
    await m.answer(VIDEO_MSG, reply_markup=VIDEO_KB)


@router.message(F.text.in_(DIRECTIONS))
async def video_pick(m: Message, state: FSMContext):
    await start_order(m, state, m.text)


@router.message(F.text == BTN_SWAP)
async def swap_open(m: Message, state: FSMContext):
    await start_order(m, state, SWAP_DIR)


@router.message(F.text.startswith(BTN_V_NEW))
async def order_new(m: Message, state: FSMContext):
    name = m.text[len(BTN_V_NEW):]
    direction = next((x for x in DIRECTIONS + [SWAP_DIR] if x.endswith(name)), None)
    if direction:
        await start_order(m, state, direction)


@router.message(F.text == BTN_V_FILES)
async def order_files(m: Message, state: FSMContext):
    d = await state.get_data()
    if "direction" not in d:
        await m.answer("Avval yo'nalishni tanlang:", reply_markup=VIDEO_KB)
        return
    have, total, _ = progress_of(d)
    await m.answer(f"📦 Qabul qilingan fayllar: {have}/{total}\n\n" + next_prompt(d),
                   reply_markup=order_kb(d["direction"]))


@router.message(F.text == BTN_V_CANCEL)
async def order_cancel(m: Message, state: FSMContext):
    await state.clear()
    await m.answer("❌ Buyurtma bekor qilindi.\n\nYo'nalishni tanlang:", reply_markup=VIDEO_KB)


@router.message(F.text == BTN_V_MAKE)
async def order_make(m: Message, state: FSMContext):
    d = await state.get_data()
    if "direction" not in d:
        await m.answer("Avval yo'nalishni tanlang:", reply_markup=VIDEO_KB)
        return
    _, _, miss = progress_of(d)
    if miss:
        await m.answer("Hali yetishmayapti: " + ", ".join(miss) + ".\n\n" + next_prompt(d),
                       reply_markup=order_kb(d["direction"]))
        return
    if m.from_user.id in RENDERING:
        await m.answer("⏳ Sizning videongiz hozir tayyorlanmoqda, iltimos kuting.")
        return
    if d["kind"] == "swap" and not d.get("order"):
        d["order"] = new_order()
        await state.set_data(d)
    await m.answer(confirm_text(d, m.from_user.id),
                   reply_markup=CONFIRM_SWAP_KB if d["kind"] == "swap" else CONFIRM_KB)


# --- Tasdiqlash tugmalari (inline)
@router.callback_query(F.data == "svc_ok")
async def svc_confirm(cb: CallbackQuery, state: FSMContext):
    d = await state.get_data()
    uid, chat_id = cb.from_user.id, cb.message.chat.id
    if d.get("kind") == "analysis" and d.get("link") and (d["notes"] or d["shots"]):
        kind = "analysis"
    elif d.get("key") and d.get("text"):
        kind = "service"
    else:
        await cb.answer("Buyurtma topilmadi. Qaytadan boshlang.", show_alert=True)
        return
    src = await consume(uid, "s")
    if not src:
        await cb.answer("Bugungi limit tugadi.", show_alert=True)
        await cb.message.answer(LIMIT_MSG)
        return
    await cb.answer()
    await state.clear()
    try:
        await cb.message.edit_text(ACCEPTED_MSG)
    except Exception:
        pass
    bot = cb.bot
    if kind == "analysis":
        spawn(analysis_job(bot, chat_id, uid, src, d))
    elif SERVICES[d["key"]].get("design"):
        spawn(design_job(bot, chat_id, uid, src, d["key"], d["text"]))
    else:
        spawn(text_job(bot, chat_id, uid, src, d["key"], d["text"]))


@router.callback_query(F.data == "svc_no")
async def svc_decline(cb: CallbackQuery):
    await cb.answer()
    try:
        await cb.message.edit_text("Buyurtma bekor qilindi.")
    except Exception:
        pass


@router.callback_query(F.data == "order_ok")
async def order_confirm(cb: CallbackQuery, state: FSMContext):
    d = await state.get_data()
    uid = cb.from_user.id
    if "direction" not in d or progress_of(d)[2] or (d["kind"] == "swap" and not d.get("order")):
        await cb.answer("Buyurtma topilmadi yoki fayllar to'liq emas.", show_alert=True)
        return
    if uid in RENDERING:
        await cb.answer("Video allaqachon tayyorlanmoqda.", show_alert=True)
        return
    src = await consume(uid, "s")
    if not src:
        await cb.answer("Bugungi limit tugadi.", show_alert=True)
        await cb.message.answer(LIMIT_MSG)
        return
    await cb.answer()
    await state.clear()
    try:
        await cb.message.edit_text(ACCEPTED_MSG)
    except Exception:
        pass
    spawn(run_order(cb.bot, cb.message.chat.id, uid, src, d))


@router.callback_query(F.data == "swap_re")
async def swap_reshuffle(cb: CallbackQuery, state: FSMContext):
    d = await state.get_data()
    await cb.answer()
    if d.get("kind") != "swap":
        return
    d["order"] = new_order(d.get("order"))
    await state.set_data(d)
    try:
        await cb.message.edit_text(confirm_text(d, cb.from_user.id), reply_markup=CONFIRM_SWAP_KB)
    except Exception:
        pass


@router.callback_query(F.data == "order_no")
async def order_decline(cb: CallbackQuery):
    await cb.answer()
    try:
        await cb.message.edit_text("Tasdiqlash bekor qilindi. Fayllar saqlangan, tayyor bo'lsangiz "
                                   "«Videoni tayyorlash» tugmasini qayta bosing.")
    except Exception:
        pass


# ---------- Holatga bog'liq xabarlar ----------
@router.message(Flow.qa, F.text)
async def qa(m: Message):
    await answer_with_ai(m, "q", QA_SYSTEM, m.text)


@router.message(Flow.service, F.text)
async def service_text(m: Message, state: FSMContext):
    d = await state.get_data()
    key = d.get("key")
    if key not in SERVICES:
        await m.answer(CHANNEL_MSG, reply_markup=CHANNEL_KB)
        return
    d["text"] = m.text
    await state.set_data(d)
    await m.answer(f"{SERVICES[key]['btn']}\n\n{m.text[:400]}\n\n{CONFIRM_TAIL}", reply_markup=CONFIRM_SVC_KB)


@router.message(Flow.analysis, F.text)
async def analysis_text(m: Message, state: FSMContext):
    d = await state.get_data()
    link = YT_RE.search(m.text)
    if link and not d["link"]:
        d["link"] = link.group(0)
        rest = m.text.replace(link.group(0), "").strip()
        if rest:
            d["notes"].append(rest)
        await state.set_data(d)
        await m.answer("✅ Havola qabul qilindi.\n\n" + analysis_prompt(d), reply_markup=ANALYSIS_KB)
    elif not d["link"]:
        await m.answer("Avval YouTube kanal havolasini yuboring.", reply_markup=ANALYSIS_KB)
    else:
        d["notes"].append(m.text)
        await state.set_data(d)
        await m.answer("✅ Ma'lumot qabul qilindi.\n\n" + analysis_prompt(d), reply_markup=ANALYSIS_KB)


@router.message(Flow.analysis, F.photo)
async def analysis_photo(m: Message, state: FSMContext):
    async with ORDER_LOCKS[m.from_user.id]:
        d = await state.get_data()
        if not analysis.vision_enabled():
            text = ("Hozircha skrinshotlardan o'qiy olmayman. Raqamlarni matn qilib yozing: obunachilar, "
                    "ko'rishlar, CTR, o'rtacha ko'rish davomiyligi.")
        elif not d["link"]:
            text = "Avval YouTube kanal havolasini yuboring."
        elif len(d["shots"]) >= MAX_SHOTS:
            text = f"Skrinshotlar to'liq ({MAX_SHOTS}/{MAX_SHOTS}). «Tahlil qilish»ni bosing."
        else:
            d["shots"].append(m.photo[-1].file_id)
            await state.set_data(d)
            n = len(d["shots"])
            text = (f"Skrinshot qabul qilindi: {n}/{MAX_SHOTS}. Endi «Tahlil qilish»ni bosing." if n == MAX_SHOTS
                    else f"Skrinshot qabul qilindi: {n}/{MAX_SHOTS}. Yana skrinshot yuboring yoki «Tahlil qilish»ni bosing.")
    await m.answer(text, reply_markup=ANALYSIS_KB)


@router.message(Flow.video, F.photo | F.video | F.document | F.audio | F.voice)
async def order_file(m: Message, state: FSMContext):
    async with ORDER_LOCKS[m.from_user.id]:
        d = await state.get_data()
        kind = media_kind(m)
        fobj = m.photo[-1] if m.photo else (m.video or m.audio or m.voice or m.document)
        if not m.photo and (fobj.file_size or 0) > max_mb() * 1024 * 1024:
            text = (f"⚠️ Fayl hajmi {max_mb()} MB dan katta. Telegram botlari {max_mb()} MB gacha "
                    "fayl qabul qila oladi. Kichikroq (qisqaroq yoki siqilgan) fayl yuboring.\n\n"
                    + next_prompt(d))
        else:
            text = take_file(d, kind, {"fid": fobj.file_id})
            await state.set_data(d)
    await m.answer(text, reply_markup=order_kb(d["direction"]))


@router.message(Flow.video, F.text)
async def order_text(m: Message, state: FSMContext):
    d = await state.get_data()
    if d["kind"] == "swap":
        o = parse_order(m.text)
        if o:
            d["order"] = o
            await state.set_data(d)
            await m.answer(f"✅ Yangi tartib: {fmt_order(o)}\n\n" + swap_prompt(d),
                           reply_markup=order_kb(d["direction"]))
            return
    await m.answer(next_prompt(d), reply_markup=order_kb(d["direction"]))


@router.message()
async def fallback(m: Message):
    await m.answer("Quyidagi tugmalardan birini tanlang 👇", reply_markup=MAIN_KB)


# ---------- Ishga tushirish ----------
async def main():
    global BOT_USERNAME, tg_client
    await start_web()
    await init_db()
    if TG_API_ID and TG_API_HASH:
        try:
            from pyrogram import Client as TgClient
            tg_client = TgClient("tubenova", api_id=int(TG_API_ID), api_hash=TG_API_HASH,
                                 bot_token=BOT_TOKEN, in_memory=True)
            await tg_client.start()
            log.info("MTProto ulandi: katta fayllar (200 MB gacha) yoqildi")
        except Exception:
            log.exception("MTProto ulanmadi, fayl chegarasi 20 MB bo'lib qoladi")
            tg_client = None
    bot = Bot(BOT_TOKEN)
    BOT_USERNAME = (await bot.get_me()).username
    dp = Dispatcher(storage=MemoryStorage())
    dp.include_router(router)
    log.info("TubeNova AI ishga tushdi: @%s", BOT_USERNAME)
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
