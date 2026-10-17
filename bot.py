"""TubeNova AI — YouTuberlar uchun bepul Telegram bot (aiogram 3 + Groq + ffmpeg)."""
import asyncio
import base64
import io
import json
import logging
import os
import re
import secrets
import tempfile
from collections import defaultdict
from datetime import datetime
from zoneinfo import ZoneInfo

import aiosqlite
from aiogram import Bot, Dispatcher, F, Router
from aiogram.filters import CommandObject, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (BufferedInputFile, CallbackQuery, FSInputFile, InlineKeyboardButton,
                           InlineKeyboardMarkup, KeyboardButton, Message, ReplyKeyboardMarkup)
from dotenv import load_dotenv
from groq import AsyncGroq

import coffin
import media
import render

load_dotenv()
logging.basicConfig(level=logging.INFO)
log = logging.getLogger("tubenova")

# ---------- Sozlamalar (Railway Variables yoki .env) ----------
BOT_TOKEN = os.environ["BOT_TOKEN"]
GROQ_API_KEY = os.environ["GROQ_API_KEY"]
MODEL = os.getenv("MODEL", "openai/gpt-oss-120b")
VISION_MODEL = os.getenv("VISION_MODEL", "")          # skrinshot o'qiydigan model (ixtiyoriy)
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
MAX_FILE_MB = 20           # Telegram bot yuklab ola oladigan eng katta fayl

ai = AsyncGroq(api_key=GROQ_API_KEY)
router = Router()
BOT_USERNAME = ""
ORDER_LOCKS = defaultdict(asyncio.Lock)
RENDERING = set()                    # hozir video tayyorlanayotgan foydalanuvchilar
RENDER_SEM = asyncio.Semaphore(1)    # bir vaqtda bitta video (server xotirasi uchun)

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
BTN_ANALYSIS = "📊 YouTube tahlil"
BTN_A_RUN = "👁 Tahlil qilish"
BTN_A_NEW = "🆕 Yangi tahlil"
BTN_A_CANCEL = "❌ Bekor qilish"

DIRECTIONS = ["🎮 Tiles Hop", "🕺 Coffin Dance", "👽 Alien Dance",
              "👾 Tiles Hop EXE", "🏆 GOLDEN COVER", "🎤 Say The Word", "👀 Wrong Eyes"]
READY = {"Tiles Hop", "Coffin Dance"}     # hozir ishlaydigan yo'nalishlar


def kb(rows):
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text=t) for t in r] for r in rows], resize_keyboard=True)


MAIN_KB = kb([[BTN_QA, BTN_SERVICES], [BTN_LIMITS, BTN_REF], [BTN_TOS, BTN_HELP]])
SERVICES_KB = kb([[BTN_CHANNEL, BTN_VIDEO], [BTN_MAIN]])
VIDEO_KB = kb([DIRECTIONS[i:i + 2] for i in range(0, len(DIRECTIONS), 2)] + [[BTN_SERVICES], [BTN_MAIN]])
ANALYSIS_KB = kb([[BTN_A_RUN, BTN_A_NEW], [BTN_A_CANCEL], [BTN_MAIN]])


def order_kb(direction: str):
    name = direction.split(" ", 1)[1]
    return kb([[BTN_V_MAKE, BTN_V_FILES], [BTN_V_CANCEL, BTN_V_NEW + name], [BTN_MAIN]])


# ---------- Kanal ochish xizmatlari ----------
BASE = ("Sen TubeNova AI — YouTuberlar uchun yordamchisan. Javobni o'zbek tilida "
        "(lotin yozuvida) ber, agar foydalanuvchi boshqa tilda yozmasa. Aniq va amaliy yoz. "
        "Markdown belgilarini (**, #, ```) HECH QACHON ishlatma: faqat oddiy matn va emoji.")

ASK_TAIL = "\n\nJavoblarni bitta xabarda yozib yuboring."
SERVICES = {
    "logo": {
        "btn": "🎨 Logo", "design": True,
        "ask": "Logo uchun:\n1) Kanal nomi nima?\n2) Yo'nalishi qanday?\n"
               "3) Qaysi rang va uslub yoqadi? (ixtiyoriy)" + ASK_TAIL,
    },
    "banner": {
        "btn": "🖼 Banner", "design": True,
        "ask": "Banner uchun:\n1) Kanal nomi nima?\n2) Yo'nalishi qanday?\n"
               "3) Bannerda qanday yozuv va rang bo'lsin? (ixtiyoriy)" + ASK_TAIL,
    },
    "tags": {
        "btn": "🏷 Teglar",
        "ask": "Teglar uchun:\n1) Kanal nomi nima?\n2) Videolar qaysi mavzuda?\n"
               "3) Teglar qaysi tilda bo'lsin?" + ASK_TAIL,
        "system": BASE + " Faqat foydalanuvchi aytgan ma'lumotlarga (kanal nomi, mavzu, til) tayanib 20 ta "
                  "samarali YouTube tegi yoz. Ma'lumotni o'zingdan to'qima. Teglar foydalanuvchi so'ragan "
                  "tilda bo'lsin (ikki til aytilsa, ikkalasida). Avval keng, keyin aniq teglar, vergul "
                  "bilan ajratilgan bitta qatorda; jami uzunlik 500 belgidan oshmasin.",
    },
    "about": {
        "btn": "📝 Kanal tavsifi",
        "ask": "Kanal tavsifi uchun:\n1) Kanal nomi nima?\n2) Kanalda nimalar ko'rsatiladi?\n"
               "3) Qaysi tilda va kimlar uchun yozamiz?" + ASK_TAIL,
        "system": BASE + " Kanal uchun jozibali 'About' tavsifini yoz (1000 belgigacha), faqat foydalanuvchi "
                  "aytgan ma'lumotlar asosida va u so'ragan tilda; faktlarni to'qima. Tuzilishi: "
                  "qiziqarli kirish, kanal nima haqida, kim uchun, obunaga chaqiriq. Kalit so'zlarni tabiiy kirit.",
    },
}
SERVICE_BY_BTN = {s["btn"]: k for k, s in SERVICES.items()}
CHANNEL_KB = kb([[SERVICES["logo"]["btn"], SERVICES["banner"]["btn"]],
                 [SERVICES["tags"]["btn"], SERVICES["about"]["btn"]],
                 [BTN_ANALYSIS], [BTN_MAIN]])
CHANNEL_MSG = "Kanalingiz uchun nima kerak?\n\nLogo, banner, teglar, kanal tavsifi yoki YouTube tahlilini tanlang."

DESIGN_SYSTEM = (
    "You are an expert logo and YouTube banner designer. From the user's details (any language) "
    "return ONLY a JSON object, no other text: "
    '{"name": "<channel name exactly as the user wrote it>", '
    '"text": "<exact text to print on the banner; the channel name if the user did not ask for other text>", '
    '"colors": ["#RRGGBB", "#RRGGBB"], "prompt": "<English image-generation prompt>"}. '
    "colors = the colors the user asked for (hex). "
    "The prompt must describe artwork only: subject, composition, art style, colors, lighting. "
    "NEVER include any text, letters, words or real brand logos in the artwork. ")
DESIGN_HINT = {
    "logo": "Make a LOGO: one centered emblem or mascot icon on a clean simple background with "
            "generous margin, bold shapes, suitable for a circular crop, polished vector or 3D style.",
    "banner": "Make a YouTube BANNER background: wide panoramic 16:9 scene, key elements spread to the "
              "sides, calm empty space in the center for the channel title, rich cinematic colors.",
}
ANALYSIS_SYSTEM = (BASE + " Berilgan raqamlar asosida kanalni tahlil qil: kuchli tomonlar, zaif tomonlar "
                   "va o'sish uchun 5 ta aniq qadam. Kanalga kira olmaysan, havola faqat kanalni "
                   "aniqlash uchun. Raqam yetishmasa, taxmin qilma — nimani qo'shimcha yuborish kerakligini ayt.")
QA_SYSTEM = BASE + (" Faqat YouTube, kontent yaratish, video tahrirlash va kanal o'stirish "
                    "mavzularida javob ber. Boshqa mavzuda xushmuomalalik bilan YouTube'ga qaytar.")

WELCOME = ("Assalomu alaykum! TubeNova AI yordamchisiga xush kelibsiz.\n\n"
           "Sizga qanday yordam kerak? Quyidagi tugmalardan tanlang.")
SERVICES_MSG = "🛍 Xizmatlar\n\nKerakli xizmatni tanlang:\n\n📺 Kanal ochish\n🎬 Video tayyorlash"
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


async def send_long(m: Message, text: str, **kw):
    for i in range(0, len(text), 4000):
        await m.answer(text[i:i + 4000], **kw)


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
    await send_long(m, clean(text) or "Javob bo'sh chiqdi, iltimos qayta yozing.")
    return True


def parse_json(text: str) -> dict:
    mt = re.search(r"\{.*\}", text, re.S)
    try:
        return json.loads(mt.group(0)) if mt else {}
    except Exception:
        return {}


async def make_design(m: Message, kind: str, text: str) -> bool:
    """Logo yoki banner RASMini yaratadi: avval AI rasm, bo'lmasa nom va ranglar asosida zaxira dizayn."""
    src = await consume(m.from_user.id, "s")
    if not src:
        await m.answer(LIMIT_MSG)
        return False
    await m.answer("🎨 Tayyorlanmoqda, 30–60 soniya kuting...")
    await m.bot.send_chat_action(m.chat.id, "upload_photo")
    info = {}
    try:
        info = parse_json(await ask_ai(DESIGN_SYSTEM + DESIGN_HINT[kind], text[:1500], 2000))
    except Exception:
        log.exception("Dizayn tavsifi xatosi")
    first = text.strip().splitlines()[0] if text.strip() else "Kanal"
    name = str(info.get("name") or first)[:40].strip()
    title = str(info.get("text") or name)[:40].strip() if kind == "banner" else name
    palette = media.make_palette(media.colors_from_text(text), info.get("colors"))
    prompt = str(info.get("prompt") or text)
    fallback = False
    try:
        if kind == "logo":
            img = await media.fetch_image(prompt, 1024, 1024)
        else:
            img = media.banner_with_text(await media.fetch_image(prompt, 1280, 720), title)
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
        await m.answer_photo(BufferedInputFile(data, fname), caption=cap)
        await m.answer_document(BufferedInputFile(data, fname),
                                caption="Asl fayl (sifati buzilmagan). Yoqmasa, xizmatni qayta tanlab, "
                                        "boshqacha tasvirlab yuboring.")
    except Exception:
        log.exception("Rasmni yuborishda xato")
        await refund(m.from_user.id, src)
        await m.answer("⚠️ Rasmni yuborib bo'lmadi. Limitingiz qaytarildi, qayta urinib ko'ring.")
        return False
    return True


# ---------- Holatlar ----------
class Flow(StatesGroup):
    qa = State()
    service = State()
    video = State()
    analysis = State()


# ---------- Video buyurtma: umumiy yordamchilar ----------
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
        return (f"🎵 Musiqani yoki musiqasi bor videoni yuboring. Fayl maksimum {MAX_FILE_MB} MB. "
                "Video yuborsangiz, uning ovozi musiqa sifatida ajratib olinadi.")
    if not d["video"]:
        return f"🎬 Endi montaj uchun videoni yuboring. Video maksimum {MAX_FILE_MB} MB."
    return "✅ Hammasi tayyor. «Videoni tayyorlash» tugmasini bosing."


def next_prompt(d: dict) -> str:
    return tiles_prompt(d) if d["kind"] == "tiles" else coffin_prompt(d)


def progress_of(d: dict):
    """(qabul qilingan, jami, yetishmayotganlar)"""
    if d["kind"] == "tiles":
        imgs, vids = d["imgs"], d["vids"]
        miss = ([f"{i}-rasm" for i in range(len(imgs) + 1, N + 1)] +
                [f"{i}-video" for i in range(len(vids) + 1, N + 1)])
        return len(imgs) + len(vids), 2 * N, miss
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
    if name == "Tiles Hop":
        d = {"kind": "tiles", "direction": direction, "imgs": [], "vids": []}
        text = (f"{direction}\n\nTayyorlash: taxminan 2–4 daqiqa. Navbat kutish vaqti alohida ko'rsatiladi.\n\n"
                f"Avval {N} ta rasm, keyin shu tartibdagi {N} ta coffin video yuboring.\n\n" + tiles_prompt(
                    {"imgs": [], "vids": []}))
    else:
        d = {"kind": "coffin", "direction": direction, "music": None, "video": None}
        text = COFFIN_INTRO + coffin_prompt(d)
    await state.set_state(Flow.video)
    await state.set_data(d)
    await m.answer(text, reply_markup=order_kb(direction))


CONFIRM_KB = InlineKeyboardMarkup(inline_keyboard=[[
    InlineKeyboardButton(text="✅ Tasdiqlash", callback_data="order_ok"),
    InlineKeyboardButton(text="❌ Bekor qilish", callback_data="order_no")]])


def confirm_text(d: dict, uid: int) -> str:
    title = d["direction"].split(" ", 1)[1]
    cost = "" if uid in UNLIMITED else "\n\nTasdiqlansa, 1 ta xizmat urinishi sarflanadi."
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
            path = os.path.join(tmp, f"v{i}.mp4")
            await bot.download(it["fid"], destination=path, timeout=600)
            vpaths.append(path)
        for i, it in enumerate(d["imgs"]):
            done += 1
            await step(f"📥 Fayllar yuklab olinmoqda ({done}/{total_files})...")
            path = os.path.join(tmp, f"i{i}")
            await bot.download(it["fid"], destination=path, timeout=120)
            ipaths.append(path)
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
        mp = os.path.join(tmp, "music")
        await bot.download(d["music"]["fid"], destination=mp, timeout=600)
        await step("📥 Fayllar yuklab olinmoqda (2/2)...")
        vp = os.path.join(tmp, "src")
        await bot.download(d["video"]["fid"], destination=vp, timeout=600)
        out = os.path.join(tmp, "coffin.mp4")
        total = await coffin.build(mp, vp, out, tmp, progress=step)
        await step("📤 Yuborilmoqda (video hajmiga qarab bir necha daqiqa ketishi mumkin)...")
        await bot.send_video(chat_id, FSInputFile(out), caption="🕺 Coffin Dance — video tayyor!",
                             width=coffin.VW, height=coffin.VH, duration=int(total),
                             supports_streaming=True, request_timeout=900)


async def run_order(bot: Bot, chat_id: int, uid: int, state: FSMContext, d: dict):
    """Tasdiqlangan buyurtma: jarayon xabarlari bilan tayyorlab, natijani yuboradi."""
    src = await consume(uid, "s")
    if not src:
        await bot.send_message(chat_id, LIMIT_MSG)
        return
    RENDERING.add(uid)
    status = await bot.send_message(
        chat_id, "⏳ Navbatda turibsiz, iltimos kuting..." if RENDER_SEM.locked() else "⏳ Boshlanmoqda...")

    async def step(text: str):
        try:
            await status.edit_text("⏳ Video tayyorlanmoqda (taxminan 2–5 daqiqa)\n\n" + text)
        except Exception:
            pass

    ok = False
    try:
        async with RENDER_SEM:
            await (tiles_job if d["kind"] == "tiles" else coffin_job)(bot, chat_id, d, step)
        ok = True
    except ValueError as e:
        await refund(uid, src)
        await bot.send_message(chat_id, f"⚠️ {e} Limitingiz qaytarildi.")
    except Exception:
        log.exception("Video tayyorlashda xato")
        await refund(uid, src)
        await bot.send_message(chat_id, "⚠️ Video tayyorlab bo'lmadi. Limitingiz qaytarildi. Fayllarni "
                                        f"tekshirib, qayta urinib ko'ring (har bir fayl {MAX_FILE_MB} MB dan oshmasin).")
    finally:
        RENDERING.discard(uid)
        try:
            await status.delete()
        except Exception:
            pass
    if ok:
        await state.clear()
        await bot.send_message(chat_id, "✅ Hammasi tayyor! Yana buyurtma berish uchun yo'nalishni tanlang:",
                               reply_markup=VIDEO_KB)


# ---------- YouTube tahlil ----------
YT_RE = re.compile(r"(?:https?://)?(?:www\.|m\.)?(?:youtube\.com|youtu\.be)/\S+", re.I)


def analysis_intro() -> str:
    if VISION_MODEL:
        return ("1-qadam. YouTube kanalingiz havolasi qanday?\n\nMasalan: https://www.youtube.com/@holitech\n"
                "Havolani yuboring, keyin Studio skrinshotlarini so'rayman.\n\n"
                "Rasmlar AI orqali tahlil qilinadi.")
    return ("1-qadam. YouTube kanalingiz havolasi qanday?\n\nMasalan: https://www.youtube.com/@holitech\n"
            "Havolani yuboring, keyin Studio raqamlarini so'rayman.")


def analysis_prompt(d: dict) -> str:
    if not d["link"]:
        return "Avval kanal havolasini yuboring."
    if VISION_MODEL:
        need = ("2-qadam. YouTube Studio skrinshotlarini yuboring (Analytics: ko'rishlar, obunachilar, CTR, "
                "o'rtacha ko'rish davomiyligi), 1–5 ta rasm. Raqamlarni matn qilib ham yozishingiz mumkin.")
    else:
        need = ("2-qadam. Studio raqamlarini matn qilib yozing: obunachilar, ko'rishlar, CTR, o'rtacha "
                "ko'rish davomiyligi va oxirgi 3–5 ta video nomi.")
    if d["notes"] or d["shots"]:
        return "Yana ma'lumot qo'shishingiz mumkin. Tayyor bo'lsa, «Tahlil qilish» tugmasini bosing."
    return need + " Tayyor bo'lgach, «Tahlil qilish» tugmasini bosing."


async def start_analysis(m: Message, state: FSMContext):
    await state.set_state(Flow.analysis)
    await state.set_data({"link": None, "notes": [], "shots": []})
    await m.answer(analysis_intro(), reply_markup=ANALYSIS_KB)


async def run_analysis(m: Message, state: FSMContext, d: dict):
    uid = m.from_user.id
    src = await consume(uid, "s")
    if not src:
        await m.answer(LIMIT_MSG)
        return
    await m.answer("📊 Tahlil qilinmoqda, bir oz kuting...")
    await m.bot.send_chat_action(m.chat.id, "typing")
    prompt = f"Kanal havolasi: {d['link']}\n"
    if d["notes"]:
        prompt += "Foydalanuvchi bergan ma'lumotlar:\n" + "\n".join(d["notes"])
    try:
        if d["shots"] and VISION_MODEL:
            content = [{"type": "text", "text": prompt + "\nQuyidagi YouTube Studio skrinshotlaridagi "
                                                         "raqamlarni o'qib, tahlil qil."}]
            for fid in d["shots"]:
                buf = io.BytesIO()
                await m.bot.download(fid, destination=buf)
                b64 = base64.b64encode(buf.getvalue()).decode()
                content.append({"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{b64}"}})
            r = await ai.chat.completions.create(
                model=VISION_MODEL, max_tokens=2000,
                messages=[{"role": "system", "content": ANALYSIS_SYSTEM}, {"role": "user", "content": content}])
            text = r.choices[0].message.content or ""
        else:
            text = await ask_ai(ANALYSIS_SYSTEM, prompt, 3000)
    except Exception:
        log.exception("Tahlil xatosi")
        await refund(uid, src)
        await m.answer("⚠️ Tahlil qilib bo'lmadi. Limitingiz qaytarildi, birozdan keyin urinib ko'ring.")
        return
    await send_long(m, clean(text) or "Javob bo'sh chiqdi, qayta urinib ko'ring.")
    await state.clear()
    await m.answer("Yana nima kerak?", reply_markup=CHANNEL_KB)


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
    await state.set_data({"key": key})
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
    if "link" not in d:
        await start_analysis(m, state)
        return
    miss = (["kanal havolasi"] if not d["link"] else []) + (
        ["Studio ma'lumotlari"] if not (d["notes"] or d["shots"]) else [])
    if miss:
        await m.answer("Hali yetishmayapti: " + ", ".join(miss) + ".\n\n" + analysis_prompt(d),
                       reply_markup=ANALYSIS_KB)
        return
    await run_analysis(m, state, d)


# --- Video tugmalari
@router.message(F.text == BTN_VIDEO)
async def video_menu(m: Message, state: FSMContext):
    await state.clear()
    await m.answer(VIDEO_MSG, reply_markup=VIDEO_KB)


@router.message(F.text.in_(DIRECTIONS))
async def video_pick(m: Message, state: FSMContext):
    await start_order(m, state, m.text)


@router.message(F.text.startswith(BTN_V_NEW))
async def order_new(m: Message, state: FSMContext):
    name = m.text[len(BTN_V_NEW):]
    direction = next((x for x in DIRECTIONS if x.endswith(name)), None)
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
        await m.answer("⏳ Video hozir tayyorlanmoqda, iltimos kuting.")
        return
    await m.answer(confirm_text(d, m.from_user.id), reply_markup=CONFIRM_KB)


@router.callback_query(F.data == "order_ok")
async def order_confirm(cb: CallbackQuery, state: FSMContext):
    d = await state.get_data()
    if "direction" not in d or progress_of(d)[2]:
        await cb.answer("Buyurtma topilmadi yoki fayllar to'liq emas.", show_alert=True)
        return
    if cb.from_user.id in RENDERING:
        await cb.answer("Video allaqachon tayyorlanmoqda.", show_alert=True)
        return
    await cb.answer()
    try:
        await cb.message.edit_text("✅ Buyurtma tasdiqlandi. Tayyorlash boshlandi...")
    except Exception:
        pass
    await run_order(cb.bot, cb.message.chat.id, cb.from_user.id, state, d)


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
async def service_run(m: Message, state: FSMContext):
    key = (await state.get_data())["key"]
    if SERVICES[key].get("design"):
        ok = await make_design(m, key, m.text)
    else:
        ok = await answer_with_ai(m, "s", SERVICES[key]["system"], m.text, max_tokens=3000)
    if ok:
        await state.clear()
        await m.answer("Yana nima kerak?", reply_markup=CHANNEL_KB)


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
    d = await state.get_data()
    if not VISION_MODEL:
        await m.answer("Hozircha skrinshotlardan o'qiy olmayman. Raqamlarni matn qilib yozing: obunachilar, "
                       "ko'rishlar, CTR, o'rtacha ko'rish davomiyligi.", reply_markup=ANALYSIS_KB)
    elif not d["link"]:
        await m.answer("Avval YouTube kanal havolasini yuboring.", reply_markup=ANALYSIS_KB)
    elif len(d["shots"]) >= 5:
        await m.answer("Skrinshotlar to'liq (5/5). «Tahlil qilish» tugmasini bosing.", reply_markup=ANALYSIS_KB)
    else:
        d["shots"].append(m.photo[-1].file_id)
        await state.set_data(d)
        await m.answer(f"✅ {len(d['shots'])}-skrinshot qabul qilindi.\n\n" + analysis_prompt(d),
                       reply_markup=ANALYSIS_KB)


@router.message(Flow.video, F.photo | F.video | F.document | F.audio | F.voice)
async def order_file(m: Message, state: FSMContext):
    async with ORDER_LOCKS[m.from_user.id]:
        d = await state.get_data()
        kind = media_kind(m)
        fobj = m.photo[-1] if m.photo else (m.video or m.audio or m.voice or m.document)
        if not m.photo and (fobj.file_size or 0) > MAX_FILE_MB * 1024 * 1024:
            text = (f"⚠️ Fayl hajmi {MAX_FILE_MB} MB dan katta. Telegram botlari {MAX_FILE_MB} MB gacha "
                    "fayl qabul qila oladi. Kichikroq (qisqaroq yoki siqilgan) fayl yuboring.\n\n"
                    + next_prompt(d))
        else:
            text = take_file(d, kind, {"fid": fobj.file_id})
            await state.set_data(d)
    await m.answer(text, reply_markup=order_kb(d["direction"]))


@router.message(Flow.video, F.text)
async def order_text(m: Message, state: FSMContext):
    d = await state.get_data()
    await m.answer(next_prompt(d), reply_markup=order_kb(d["direction"]))


@router.message()
async def fallback(m: Message):
    await m.answer("Quyidagi tugmalardan birini tanlang 👇", reply_markup=MAIN_KB)


# ---------- Ishga tushirish ----------
async def main():
    global BOT_USERNAME
    await init_db()
    bot = Bot(BOT_TOKEN)
    BOT_USERNAME = (await bot.get_me()).username
    dp = Dispatcher(storage=MemoryStorage())
    dp.include_router(router)
    log.info("TubeNova AI ishga tushdi: @%s", BOT_USERNAME)
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
