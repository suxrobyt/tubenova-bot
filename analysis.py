"""YouTube tahlil: skrinshotlarni o'qiydigan model (Gemini yoki Groq vision) + ochiq manbalar."""
import base64
import os
from datetime import datetime, timezone

import httpx

GEMINI_KEY = os.getenv("GEMINI_API_KEY", "")
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-2.5-flash")
VISION_MODEL = os.getenv("VISION_MODEL", "")

SYSTEM = (
    "You are a senior YouTube growth analyst for small gaming and Shorts channels. Write the final report in "
    "Uzbek (Latin script), plain text only: no markdown, no asterisks, no tables, no '#'. "
    "Use ONLY numbers that are visible in the screenshots or in the user's notes; never invent numbers; if "
    "something is unreadable write \"ko'rinmadi\". Mention the date range of each screenshot when visible. "
    "Be concrete and practical. If Google Search is available, find 2 active channels in the same niche, language, "
    "audience and video format and verify their subscriber counts and recent activity from the search results; "
    "if you cannot verify something, say so. Output EXACTLY this structure:\n"
    "YouTube tahlil\n<channel link>\nTekshiruv: <the date given by the user>\nSkrinshot davrlari:\n"
    "1-skrinshot: <screen name and period>\n(one line per screenshot; if there are no screenshots write "
    "'Skrinshot yo'q, raqamlar foydalanuvchi matnidan olindi')\n\n"
    "1) Muhim raqamlar va davr\n- ...\n"
    "2) Kunlik yuklash va prosmotr dinamikasi\n- ...\n"
    "3) Aynan shu yo'nalish/til/auditoriya/video formatiga mos kuchli namuna kanal\n- ...\n"
    "4) Yuklash jadvali va keyingi qadam\n- ...\n"
    "Do not write a sources section; it is added automatically.")


def vision_enabled() -> bool:
    return bool(GEMINI_KEY or VISION_MODEL)


def _prompt(link: str, notes: list, n: int) -> str:
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    txt = f"Channel link: {link}\nDate: {now}\nNumber of screenshots: {n}\n"
    if notes:
        txt += "User notes (country, time zone, numbers, etc.):\n" + "\n".join(notes) + "\n"
    return txt


async def _gemini(prompt: str, images: list):
    parts = [{"text": prompt}] + [
        {"inline_data": {"mime_type": "image/jpeg", "data": base64.b64encode(b).decode()}} for b in images]
    body = {"systemInstruction": {"parts": [{"text": SYSTEM}]},
            "contents": [{"role": "user", "parts": parts}],
            "generationConfig": {"maxOutputTokens": 6000}}
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{GEMINI_MODEL}:generateContent"
    async with httpx.AsyncClient(timeout=180) as client:
        r = await client.post(url, headers={"x-goog-api-key": GEMINI_KEY},
                              json={**body, "tools": [{"google_search": {}}]})
        if r.status_code == 400:               # qidiruv vositasi bilan ishlamasa, vositasiz urinib ko'ramiz
            r = await client.post(url, headers={"x-goog-api-key": GEMINI_KEY}, json=body)
        r.raise_for_status()
    cand = r.json()["candidates"][0]
    text = "".join(p.get("text", "") for p in cand.get("content", {}).get("parts", []))
    sources = []
    for c in cand.get("groundingMetadata", {}).get("groundingChunks", []):
        w = c.get("web")
        if w and w.get("uri") and (w.get("title"), w["uri"]) not in sources:
            sources.append((w.get("title", ""), w["uri"]))
    return text, sources[:6]


async def _groq_vision(client, prompt: str, images: list):
    content = [{"type": "text", "text": prompt}] + [
        {"type": "image_url", "image_url": {"url": "data:image/jpeg;base64," + base64.b64encode(b).decode()}}
        for b in images]
    r = await client.chat.completions.create(
        model=VISION_MODEL, max_tokens=3000,
        messages=[{"role": "system", "content": SYSTEM}, {"role": "user", "content": content}])
    return r.choices[0].message.content or "", []


async def analyze(groq_client, text_fn, link: str, notes: list, images: list):
    """Tahlil matni va manbalar ro'yxatini qaytaradi. Skrinshot o'qiydigan model bo'lmasa, faqat matn bilan ishlaydi."""
    prompt = _prompt(link, notes, len(images))
    if images and GEMINI_KEY:
        try:
            return await _gemini(prompt, images)
        except Exception:
            if not VISION_MODEL:
                raise
    if images and VISION_MODEL:
        return await _groq_vision(groq_client, prompt, images)
    return await text_fn(SYSTEM, prompt), []


def with_sources(text: str, sources: list) -> str:
    if not sources:
        return text
    lines = ["", "Ochiq manbalar:"]
    for i, (title, uri) in enumerate(sources, 1):
        lines.append(f"[{i}] {title}\n{uri}\n")
    return text.rstrip() + "\n" + "\n".join(lines)
