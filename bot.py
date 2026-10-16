"""TubeNova AI — YouTuberlar uchun bepul Telegram bot (aiogram 3 + Groq + Pollinations)."""
import asyncio
import json
import logging
import os
import re
import secrets
from collections import defaultdict
from datetime import datetime
from zoneinfo import ZoneInfo

import aiosqlite
from aiogram import Bot, Dispatcher, F, Router
from aiogram.filters import CommandObject, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (BufferedInputFile, InlineKeyboardButton,
                           InlineKeyboardMarkup, KeyboardButton, Message,
                           ReplyKeyboardMarkup)
from dotenv import load_dotenv
from groq import AsyncGroq

import media

load_dotenv()
logging.basicConfig(level=logging.INFO)
log = logging.getLogger("tubenova")

# ---------- Sozlamalar (Railway Variables yoki .env) ----------
BOT_TOKEN = os.environ["BOT_TOKEN"]
GROQ_API_KEY = os.environ["GROQ_API_KEY"]
MODEL = os.getenv("MODEL", "openai/gpt-oss-120b")
ADMIN_ID = int(os.getenv("ADMIN_ID", "0"))           # buyurtmalar shu odamga boradi
ADMIN_USERNAME = os.getenv("ADMIN_USERNAME", "")      # @ belgisisiz
DAILY_Q = int(os.getenv("DAILY_QUESTIONS", "15"))
DAILY_S = int(os.getenv("DAILY_SERVICES", "3"))
REF_BONUS = int(os.getenv("REF_BONUS", "2"))
# Cheklovsiz foydalanuvchilar: ADMIN_ID + UNLIMITED_IDS (vergul bilan, raqamlar)
UNLIMITED = {int(x) for x in os.getenv("UNLIMITED_IDS", "").replace(" ", "").split(",") if x.isdigit()}
if ADMIN_ID:
    UNLIMITED.add(ADMIN_ID)
DB = "tubenova.db"
TZ = ZoneInfo("Asia/Tashkent")
N = 4  # buyurtmadagi rasm va video soni

ai = AsyncGroq(api_key=GROQ_API_KEY)
router = Router()
BOT_USERNAME = ""
ORDER_LOCKS = defaultdict(asyncio.Lock)

# ---------- Tugmalar ----------
BTN_QA = "💬 Savol-Javob 24/7"
BTN_CHANNEL = "📺 Kanal yaratish"
BTN_VIDEO = "🎬 Video tayyorlash"
BTN_LIMITS = "🎁 Limitlarim"
BTN_REF = "👥 Do'st taklif qilish"
BTN_TOS = "📜 Shartlar"
BTN_HELP = "🆘 Yordam"
BTN_MAIN = "↩️ Asosiy menyu"
BTN_V_MAKE = "▶️ Videoni tayyorlash"
BTN_V_FILES = "📋 Yuborilgan fayllarni ko'rish"
BTN_V_CANCEL = "❌ Buyurtmani bekor qilish"
BTN_V_NEW = "🔄 Yangi "

DIRECTIONS = ["🎮 Tiles Hop", "🕺 Coffin Dance", "👽 Alien Dance",
              "👾 Tiles Hop EXE", "🏆 GOLDEN COVER", "🎤 Say The Word", "👀 Wrong Eyes"]


def kb(rows):
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text=t) for t in r] for r in rows],
        resize_keyboard=True)


MAIN_KB = kb([[BTN_QA, BTN_CHANNEL], [BTN_VIDEO, BTN_LIMITS],
              [BTN_REF], [BTN_TOS, BTN_HELP]])
VIDEO_KB = kb([DIRECTIONS[i:i + 2] for i in range(0, len(DIRECTIONS), 2)] + [[BTN_MAIN]])


def order_kb(direction: str):
    name = direction.split(" ", 1)[1]
    return kb([[BTN_V_MAKE, BTN_V_FILES], [BTN_V_CANCEL, BTN_V_NEW + name], [BTN_MAIN]])


# ---------- Kanal xizmatlari ----------
BASE = ("Sen TubeNova AI — YouTuberlar uchun yordamchisan. Javobni o'zbek tilida "
        "(lotin yozuvida) ber, agar foydalanuvchi boshqa tilda yozmasa. Aniq va amaliy yoz. "
        "Markdown belgilarini (**, #, ```) ishlatma, oddiy matn va emoji ishlat.")

SERVICES = {
    "logo": {
        "btn": "🎨 Logo", "design": True,
        "ask": "Logo uchun bitta xabarda yozing:\n1) Kanal nomi?\n2) Yo'nalishi qanday?\n"
               "3) Qaysi rang va uslub yoqadi? (ixtiyoriy)\n\nLogo rasm sifatida tayyorlanadi.",
    },
    "banner": {
        "btn": "🖼 Banner", "design": True,
        "ask": "Banner uchun bitta xabarda yozing:\n1) Kanal nomi?\n2) Yo'nalishi qanday?\n"
               "3) Qaysi rang va uslub yoqadi? (ixtiyoriy)\n\nBanner rasm sifatida tayyorlanadi, "
               "kanal nomi o'rtasiga yoziladi.",
    },
    "tags": {
        "btn": "🏷 Teglar",
        "ask": "Teglar uchun bitta xabarda yozing:\n1) Kanal nomi?\n2) Videolar qaysi mavzuda?\n"
               "3) Teglar qaysi tilda bo'lsin?",
        "system": BASE + " Berilgan ma'lumot bo'yicha 20 ta samarali YouTube tegi yoz: "
                  "avval keng, keyin aniq teglar, vergul bilan ajratilgan bitta qatorda.",
    },
    "about": {
        "btn": "📝 Kanal tavsifi",
        "ask": "Kanal tavsifi uchun bitta xabarda yozing:\n1) Kanal nomi?\n"
               "2) Kanalda nimalar ko'rsatiladi?\n3) Qaysi tilda va kimlar uchun yozamiz?",
        "system": BASE + " Kanal uchun jozibali 'About' tavsifini yoz (1000 belgigacha): "
                  "qiziqarli kirish, kanal nima haqida, qachon video chiqishi, obunaga chaqiriq.",
    },
    "analysis": {
        "btn": "📊 Kanal tahlili",
        "ask": "Kanal tahlili uchun yuboring:\n1) Kanal havolasi\n2) Studio raqamlari: obunachilar, "
               "ko'rishlar, CTR, o'rtacha ko'rish davomiyligi\n3) Oxirgi 3-5 ta video nomi\n"
               "Hammasini bitta xabarda yozing.",
        "system": BASE + " Berilgan raqamlar asosida kanalni tahlil qil: kuchli tomonlar, "
                  "zaif tomonlar va o'sish uchun 5 ta aniq qadam. Raqam yetishmasa, "
                  "taxmin qilma — nimani qo'shimcha yuborish kerakligini ayt.",
    },
}
SERVICE_BY_BTN = {s["btn"]: k for k, s in SERVICES.items()}
CHANNEL_KB = kb([[SERVICES["logo"]["btn"], SERVICES["banner"]["btn"]],
                 [SERVICES["tags"]["btn"], SERVICES["about"]["btn"]],
                 [SERVICES["analysis"]["btn"]], [BTN_MAIN]])

DESIGN_SYSTEM = (
    "You are an expert logo and YouTube banner designer. From the user's details (any language) "
    "return ONLY a JSON object, no other text: "
    '{"name": "<channel name exactly as the user wrote it>", "prompt": "<English image-generation prompt>"}. '
    "The prompt must describe artwork only: subject, composition, art style, colors, lighting. "
    "NEVER include any text, letters, words or real brand logos in the artwork. ")
DESIGN_HINT = {
    "logo": "Make a LOGO: one centered emblem or mascot icon on a clean simple background with "
            "generous margin, bold shapes, suitable for a circular crop, polished vector or 3D style.",
    "banner": "Make a YouTube BANNER background: wide panoramic 16:9 scene, key elements spread to the "
              "sides, calm empty space in the center for the channel title, rich cinematic colors.",
}

QA_SYSTEM = BASE + (" Faqat YouTube, kontent yaratish, video tahrirlash va kanal o'stirish "
                    "mavzularida javob ber. Boshqa mavzuda xushmuomalalik bilan YouTube'ga qaytar.")

WELCOME = ("Assalomu alaykum! TubeNova AI yordamchisiga xush kelibsiz.\n\n"
           "Sizga qanday yordam kerak? Quyidagi tugmalardan tanlang.")
LIMIT_MSG = ("⏳ Bugungi limit tugadi. Limit har kuni 00:00 da (Toshkent vaqti) yangilanadi.\n"
             "Do'stlaringizni taklif qilib bonus urinishlar oling: «" + BTN_REF + "».")
TOS_TEXT = ("📜 Foydalanish shartlari\n\n"
            "• Bot bepul, kunlik limitlar bilan ishlaydi.\n"
            "• AI javoblari yo'l-yo'riq sifatida beriladi, natija kafolatlanmaydi.\n"
            "• Qonunga zid yoki boshqalarning huquqini buzadigan so'rovlar qabul qilinmaydi.\n"
            "• Yuborgan rasm va matnlaringiz faqat xizmat ko'rsatish uchun ishlatiladi.")


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
            await db.execute("UPDATE users SET day=?, q_used=0, s_used=0 WHERE id=?",
                             (today(), uid))
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
        messages=[{"role": "system", "content": system},
                  {"role": "user", "content": prompt}], **kw)
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
    await send_long(m, text or "Javob bo'sh chiqdi, iltimos qayta yozing.")
    return True


def parse_json(text: str) -> dict:
    mt = re.search(r"\{.*\}", text, re.S)
    try:
        return json.loads(mt.group(0)) if mt else {}
    except Exception:
        return {}


async def make_design(m: Message, kind: str, text: str) -> bool:
    """Logo yoki banner RASMini yaratadi (matn emas)."""
    src = await consume(m.from_user.id, "s")
    if not src:
        await m.answer(LIMIT_MSG)
        return False
    await m.answer("🎨 Tayyorlanmoqda, 30–60 soniya kuting...")
    try:
        await m.bot.send_chat_action(m.chat.id, "upload_photo")
        raw = await ask_ai(DESIGN_SYSTEM + DESIGN_HINT[kind], text[:1500], 2000)
        info = parse_json(raw)
        name = str(info.get("name") or text.strip().splitlines()[0])[:40].strip()
        prompt = str(info.get("prompt") or text)
        if kind == "logo":
            img = await media.fetch_image(prompt, 1024, 1024)
            data, fname = media.to_bytes(img, "PNG"), "logo.png"
        else:
            img = await media.fetch_image(prompt, 1280, 720)
            img = media.banner_with_text(img, name)
            data, fname = media.to_bytes(img, "JPEG"), "banner.jpg"
    except Exception:
        log.exception("Dizayn xatosi")
        await refund(m.from_user.id, src)
        await m.answer("⚠️ Rasm yaratib bo'lmadi. Limitingiz qaytarildi, birozdan keyin qayta urinib ko'ring.")
        return False
    title = "🎨 Logo tayyor!" if kind == "logo" else "🖼 Banner tayyor (2560×1440)!"
    await m.answer_photo(BufferedInputFile(data, fname), caption=title)
    await m.answer_document(BufferedInputFile(data, fname),
                            caption="Asl fayl (sifati buzilmagan). Yoqmasa, xizmatni qayta tanlab, "
                                    "boshqacha tasvirlab yuboring.")
    return True


# ---------- Video buyurtma yordamchilari ----------
def next_prompt(imgs, vids) -> str:
    if len(imgs) < N:
        return (f"🖼 {len(imgs) + 1}-qahramon rasmini yuboring. Qahramon to'liq ko'rinsin. "
                "Fon avtomatik olib tashlanadi, PNG o'lchami moslanadi.")
    if len(vids) < N:
        return f"🎞 {len(vids) + 1}-coffin videoni yuboring."
    return "✅ Hammasi tayyor. «Videoni tayyorlash» tugmasini bosing."


def missing(imgs, vids) -> list:
    return ([f"{i}-rasm" for i in range(len(imgs) + 1, N + 1)] +
            [f"{i}-video" for i in range(len(vids) + 1, N + 1)])


def media_kind(m: Message):
    if m.photo:
        return "img"
    if m.video:
        return "vid"
    d = m.document
    if d and d.mime_type:
        if d.mime_type.startswith("image/"):
            return "img"
        if d.mime_type.startswith("video/"):
            return "vid"
    return None


async def start_order(m: Message, state: FSMContext, direction: str):
    await state.set_state(Flow.video)
    await state.update_data(direction=direction, imgs=[], vids=[])
    await m.answer(
        f"{direction}\n\n"
        "Tayyorlash: taxminan 2–4 daqiqa. Navbat kutish vaqti alohida ko'rsatiladi.\n\n"
        f"Avval {N} ta rasm, keyin shu tartibdagi {N} ta coffin video yuboring.\n\n"
        + next_prompt([], []), reply_markup=order_kb(direction))


# ---------- Holatlar ----------
class Flow(StatesGroup):
    qa = State()
    service = State()
    video = State()


@router.message.outer_middleware()
async def ensure_user(handler, event, data):
    if event.from_user and not (event.text or "").startswith("/start"):
        if await get_user(event.from_user.id) is None:
            await register(event.from_user.id, event.from_user.full_name)
    return await handler(event, data)


# ---------- Asosiy menyu ----------
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
    await m.answer("Asosiy menyu:", reply_markup=MAIN_KB)


@router.message(F.text == BTN_LIMITS)
async def limits(m: Message):
    if m.from_user.id in UNLIMITED:
        await m.answer("🎁 Limitlarim\n\n♾ Sizda cheklov yo'q.")
        return
    u = await get_user(m.from_user.id)
    await m.answer(
        "🎁 Limitlarim\n\n"
        f"💬 Savollar: {DAILY_Q - u['q_used']}/{DAILY_Q}\n"
        f"🛠 Xizmatlar: {max(DAILY_S - u['s_used'], 0)}/{DAILY_S}\n"
        f"⭐ Bonus urinishlar: {u['bonus']}\n\n"
        "Kunlik limit har kuni 00:00 da (Toshkent vaqti) yangilanadi.")


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
            InlineKeyboardButton(text="🆘 Adminga yozish", url=f"https://t.me/{ADMIN_USERNAME}")]])
    await m.answer("✉️ Admin bilan bog'lanish uchun quyidagi tugmani bosing.\n"
                   "Admin murojaatingizni 24 soat ichida ko'rib chiqadi.", reply_markup=markup)


# ---------- Savol-Javob, kanal xizmatlari, video: tugmalar ----------
@router.message(F.text == BTN_QA)
async def qa_start(m: Message, state: FSMContext):
    await state.set_state(Flow.qa)
    await m.answer(f"YouTube bo'yicha qanday savolingiz bor?\n\nKuniga {DAILY_Q} ta savol bepul. "
                   "Savolingizni qisqa va aniq yozing.", reply_markup=kb([[BTN_MAIN]]))


@router.message(F.text == BTN_CHANNEL)
async def channel_menu(m: Message, state: FSMContext):
    await state.clear()
    await m.answer("Kanalingiz uchun nima kerak?", reply_markup=CHANNEL_KB)


@router.message(F.text.in_(SERVICE_BY_BTN))
async def service_pick(m: Message, state: FSMContext):
    key = SERVICE_BY_BTN[m.text]
    await state.set_state(Flow.service)
    await state.update_data(key=key)
    await m.answer(SERVICES[key]["ask"], reply_markup=kb([[BTN_MAIN]]))


@router.message(F.text == BTN_VIDEO)
async def video_menu(m: Message, state: FSMContext):
    await state.clear()
    await m.answer("🎬 Video tayyorlash\n\nYo'nalishni tanlang:", reply_markup=VIDEO_KB)


@router.message(F.text.in_(DIRECTIONS))
async def video_pick(m: Message, state: FSMContext):
    await start_order(m, state, m.text)


@router.message(F.text.startswith(BTN_V_NEW))
async def order_new(m: Message, state: FSMContext):
    name = m.text[len(BTN_V_NEW):]
    direction = next((d for d in DIRECTIONS if d.endswith(name)), None)
    if direction:
        await start_order(m, state, direction)


@router.message(F.text == BTN_V_FILES)
async def order_files(m: Message, state: FSMContext):
    d = await state.get_data()
    if "direction" not in d:
        await m.answer("Avval yo'nalishni tanlang:", reply_markup=VIDEO_KB)
        return
    n = len(d["imgs"]) + len(d["vids"])
    await m.answer(f"📦 Qabul qilingan fayllar: {n}/{2 * N}\n\n" + next_prompt(d["imgs"], d["vids"]),
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
    imgs, vids, direction = d["imgs"], d["vids"], d["direction"]
    miss = missing(imgs, vids)
    if miss:
        await m.answer("Hali yetishmayapti: " + ", ".join(miss) + ".\n\n" + next_prompt(imgs, vids),
                       reply_markup=order_kb(direction))
        return
    if not ADMIN_ID:
        await m.answer("Video xizmati hali ulanmagan.")
        return
    src = await consume(m.from_user.id, "s")
    if not src:
        await m.answer(LIMIT_MSG)
        return
    try:
        await m.bot.send_message(
            ADMIN_ID, f"🎬 Yangi video buyurtmasi\nYo'nalish: {direction}\n"
                      f"Foydalanuvchi: {m.from_user.full_name} (id {m.from_user.id})\n"
                      f"Fayllar tartibi: {N} ta rasm, keyin {N} ta coffin video")
        for it in imgs + vids:
            await m.bot.copy_message(ADMIN_ID, it["chat"], it["msg"])
    except Exception:
        log.exception("Buyurtmani yuborishda xato")
        await refund(m.from_user.id, src)
        await m.answer("⚠️ Buyurtmani yuborib bo'lmadi. Limitingiz qaytarildi, qayta urinib ko'ring.")
        return
    await state.clear()
    await m.answer("✅ Buyurtma qabul qilindi. Video va prevyu tayyor bo'lgach, sizga yuboriladi.",
                   reply_markup=MAIN_KB)


# ---------- Holatga bog'liq xabarlar (tugmalardan KEYIN ro'yxatdan o'tadi) ----------
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


@router.message(Flow.video, F.photo | F.video | F.document)
async def order_file(m: Message, state: FSMContext):
    async with ORDER_LOCKS[m.from_user.id]:
        d = await state.get_data()
        imgs, vids, direction = d["imgs"], d["vids"], d["direction"]
        kind = media_kind(m)
        item = {"chat": m.chat.id, "msg": m.message_id}
        if kind is None:
            text = "Faqat rasm yoki video yuboring.\n\n" + next_prompt(imgs, vids)
        elif kind == "img" and len(imgs) < N:
            imgs.append(item)
            text = f"✅ {len(imgs)}-rasm qabul qilindi.\n\n" + next_prompt(imgs, vids)
        elif kind == "img":
            text = "Rasmlar to'liq.\n\n" + next_prompt(imgs, vids)
        elif len(imgs) < N:
            text = "Avval rasmlarni yuboring.\n\n" + next_prompt(imgs, vids)
        elif len(vids) < N:
            vids.append(item)
            text = f"✅ {len(vids)}-video qabul qilindi.\n\n" + next_prompt(imgs, vids)
        else:
            text = "Videolar to'liq.\n\n" + next_prompt(imgs, vids)
        await state.update_data(imgs=imgs, vids=vids)
    await m.answer(text, reply_markup=order_kb(direction))


@router.message(Flow.video, F.text)
async def order_text(m: Message, state: FSMContext):
    d = await state.get_data()
    await m.answer(next_prompt(d["imgs"], d["vids"]), reply_markup=order_kb(d["direction"]))


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
