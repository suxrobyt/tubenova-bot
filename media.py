"""Logo va banner rasmlarini yaratish: AI rasm (Pollinations) + Pillow bilan yozuv."""
import asyncio
import io
import random
import urllib.parse

import httpx
from PIL import Image, ImageDraw, ImageFont

API = "https://image.pollinations.ai/prompt/"


def _font(size: int):
    try:
        return ImageFont.load_default(size=size)
    except Exception:
        return ImageFont.load_default()


async def fetch_image(prompt: str, width: int, height: int) -> Image.Image:
    """Matn tavsifi bo'yicha AI rasm yaratadi (3 marta urinadi)."""
    last = "noma'lum xato"
    async with httpx.AsyncClient(timeout=120, follow_redirects=True) as client:
        for _ in range(3):
            url = (API + urllib.parse.quote(prompt[:900]) +
                   f"?width={width}&height={height}&seed={random.randint(1, 999999)}"
                   "&nologo=true&model=flux")
            try:
                r = await client.get(url)
                if r.status_code == 200 and r.headers.get("content-type", "").startswith("image"):
                    return Image.open(io.BytesIO(r.content)).convert("RGB")
                last = f"HTTP {r.status_code}"
            except Exception as e:  # tarmoq xatosi
                last = repr(e)
            await asyncio.sleep(5)
    raise RuntimeError(f"Rasm xizmati javob bermadi: {last}")


def banner_with_text(img: Image.Image, name: str) -> Image.Image:
    """YouTube banner (2560x1440): kanal nomi xavfsiz zona (markaz) ichiga yoziladi."""
    w, h = 2560, 1440
    img = img.convert("RGB").resize((w, h), Image.LANCZOS)
    if not name:
        return img
    draw = ImageDraw.Draw(img)
    size = 200
    try:
        f = _font(size)
        while size > 60 and draw.textlength(name, font=f) > 1400:
            size -= 10
            f = _font(size)
        draw.text((w // 2, h // 2), name, font=f, fill="white", anchor="mm",
                  stroke_width=max(4, size // 16), stroke_fill=(0, 0, 0))
    except Exception:
        draw.text((w // 2 - 200, h // 2), name, fill="white")
    return img


def to_bytes(img: Image.Image, fmt: str) -> bytes:
    buf = io.BytesIO()
    if fmt == "JPEG":
        img.save(buf, "JPEG", quality=92)
    else:
        img.save(buf, "PNG")
    return buf.getvalue()
