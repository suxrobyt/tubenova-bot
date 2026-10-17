"""Rasm yaratish: logo, banner (AI rasm + zaxira dizayn) va video prevyusi."""
import asyncio
import colorsys
import io
import random
import re
import urllib.parse

import httpx
from PIL import Image, ImageChops, ImageDraw, ImageFilter, ImageFont, ImageOps

API = "https://image.pollinations.ai/prompt/"
FONTS = ["DejaVuSans-Bold.ttf", "LiberationSans-Bold.ttf", "Arial Bold.ttf", "arialbd.ttf"]
GLOWS = [(0, 229, 255), (255, 64, 200), (90, 255, 120), (255, 170, 40)]


# ---------- Shrift va matn ----------
def _font(size: int):
    for name in FONTS:
        try:
            return ImageFont.truetype(name, size)
        except Exception:
            pass
    try:
        return ImageFont.load_default(size=size)
    except Exception:
        return ImageFont.load_default()


def fit_font(draw, text, max_w, start, min_size=40):
    size = start
    f = _font(size)
    try:
        while size > min_size and draw.textlength(text, font=f) > max_w:
            size -= 8
            f = _font(size)
    except Exception:
        pass
    return f, size


def fancy_text(draw, xy, text, font, fill, outline, bold=0, outw=0):
    """Qalin yozuv + tashqi kontur."""
    try:
        draw.text(xy, text, font=font, fill=outline, anchor="mm",
                  stroke_width=bold + outw, stroke_fill=outline)
        draw.text(xy, text, font=font, fill=fill, anchor="mm",
                  stroke_width=bold, stroke_fill=fill)
    except Exception:
        draw.text(xy, text, fill=fill)


# ---------- Ranglar ----------
COLOR_WORDS = {
    "qizil": "#e53935", "red": "#e53935", "красн": "#e53935",
    "ko'k": "#1e88e5", "kok": "#1e88e5", "blue": "#1e88e5", "син": "#1e88e5", "голуб": "#4fc3f7",
    "havorang": "#4fc3f7", "yashil": "#2e9e47", "green": "#2e9e47", "зелен": "#2e9e47",
    "sariq": "#fdd835", "yellow": "#fdd835", "желт": "#fdd835", "жёлт": "#fdd835",
    "oranj": "#fb8c00", "orange": "#fb8c00", "zarg'aldoq": "#fb8c00", "оранж": "#fb8c00",
    "binafsha": "#8e24aa", "purple": "#8e24aa", "violet": "#8e24aa", "фиолет": "#8e24aa",
    "pushti": "#ec407a", "pink": "#ec407a", "розов": "#ec407a",
    "oq": "#ffffff", "white": "#ffffff", "бел": "#ffffff",
    "qora": "#111111", "black": "#111111", "черн": "#111111", "чёрн": "#111111",
    "kulrang": "#9e9e9e", "gray": "#9e9e9e", "grey": "#9e9e9e", "сер": "#9e9e9e",
    "oltin": "#ffc107", "tilla": "#ffc107", "gold": "#ffc107", "золот": "#ffc107",
    "jigarrang": "#6d4c41", "brown": "#6d4c41", "firuza": "#00bcd4", "cyan": "#00bcd4",
    "turquoise": "#00bcd4", "neon": "#00e5ff",
}


def _norm(s: str) -> str:
    return re.sub(r"[‘’`ʻʼ´]", "'", s.lower())


def colors_from_text(text: str) -> list:
    """Foydalanuvchi yozgan rang so'zlarini (qizil, ko'k, ...) tartib bilan topadi."""
    t = _norm(text)
    found = []
    for word, hexv in COLOR_WORDS.items():
        pat = r"(?<![\w'])" + re.escape(word) + (r"\w*" if len(word) >= 4 else r"(?![\w'])")
        mt = re.search(pat, t)
        if mt:
            found.append((mt.start(), hexv))
    found.sort()
    out = []
    for _, hv in found:
        if hv not in out:
            out.append(hv)
    return out


def _rgb(h: str):
    h = h.lstrip("#")
    return tuple(int(h[i:i + 2], 16) for i in (0, 2, 4))


def _valid(c) -> bool:
    return bool(re.fullmatch(r"#[0-9a-fA-F]{6}", str(c)))


def _shift(rgb):
    h, s, v = colorsys.rgb_to_hsv(*(x / 255 for x in rgb))
    h = (h + 0.08) % 1
    v = v * 0.55 if v > 0.5 else min(1.0, v + 0.45)
    return tuple(int(x * 255) for x in colorsys.hsv_to_rgb(h, max(s, 0.5), v))


def lighten(rgb, f):
    return tuple(int(c + (255 - c) * f) for c in rgb)


def make_palette(user_colors, llm_colors) -> list:
    cols = list(user_colors)
    if not cols and isinstance(llm_colors, list):
        cols = [c for c in llm_colors if _valid(c)]
    if not cols:
        cols = ["#7c4dff"]
    pal = [_rgb(c) for c in cols[:3]]
    if len(pal) == 1:
        pal.append(_shift(pal[0]))
    return pal


def text_colors(c1, c2):
    lum = sum(0.299 * (a + b) / 2 * k for a, b, k in zip(c1, c2, (1, 1, 1)) for _ in [0]) if False else \
        0.299 * (c1[0] + c2[0]) / 2 + 0.587 * (c1[1] + c2[1]) / 2 + 0.114 * (c1[2] + c2[2]) / 2
    return ((20, 20, 30), (255, 255, 255)) if lum > 170 else ((255, 255, 255), (15, 15, 25))


def gradient(size, c1, c2):
    mask = Image.linear_gradient("L").resize(size)
    return Image.composite(Image.new("RGB", size, c2), Image.new("RGB", size, c1), mask)


def glow(img, color, strength=0.5):
    m = ImageOps.invert(Image.radial_gradient("L")).resize(img.size).point(lambda v: int(v * strength))
    return Image.composite(Image.new("RGB", img.size, color), img, m)


# ---------- AI rasm ----------
async def fetch_image(prompt: str, width: int, height: int) -> Image.Image:
    """Matn tavsifi bo'yicha AI rasm yaratadi (2 marta urinadi)."""
    last = "noma'lum xato"
    async with httpx.AsyncClient(timeout=60, follow_redirects=True) as client:
        for _ in range(2):
            url = (API + urllib.parse.quote(prompt[:900]) +
                   f"?width={width}&height={height}&seed={random.randint(1, 999999)}"
                   "&nologo=true&model=flux")
            try:
                r = await client.get(url)
                if r.status_code == 200 and r.headers.get("content-type", "").startswith("image"):
                    return Image.open(io.BytesIO(r.content)).convert("RGB")
                last = f"HTTP {r.status_code}"
            except Exception as e:
                last = repr(e)
            await asyncio.sleep(3)
    raise RuntimeError(f"Rasm xizmati javob bermadi: {last}")


def banner_with_text(img: Image.Image, name: str) -> Image.Image:
    """AI rasm ustiga kanal nomini yozadi (2560x1440, markaziy xavfsiz zona)."""
    w, h = 2560, 1440
    img = img.convert("RGB").resize((w, h), Image.LANCZOS)
    if name:
        d = ImageDraw.Draw(img)
        f, size = fit_font(d, name, 1400, 220, 70)
        fancy_text(d, (w // 2, h // 2), name, f, (255, 255, 255), (10, 10, 20),
                   bold=max(2, size // 40), outw=max(4, size // 16))
    return img


# ---------- Zaxira dizayn (AI xizmati ishlamasa ham rasm chiqadi) ----------
def _initials(name: str) -> str:
    words = re.findall(r"\w+", name, re.U)
    if not words:
        return "TN"
    if len(words) == 1:
        return words[0][0].upper()
    return (words[0][0] + words[1][0]).upper()


def draw_logo(name: str, palette) -> Image.Image:
    s = 1024
    c1, c2 = palette[0], palette[1]
    img = glow(gradient((s, s), c1, c2), lighten(c1, 0.55), 0.6).convert("RGBA")
    ov = Image.new("RGBA", (s, s), (0, 0, 0, 0))
    d = ImageDraw.Draw(ov)
    d.ellipse((60, 60, s - 60, s - 60), outline=(255, 255, 255, 210), width=14)
    d.ellipse((105, 105, s - 105, s - 105), outline=(255, 255, 255, 90), width=4)
    img = Image.alpha_composite(img, ov)
    d = ImageDraw.Draw(img)
    fg, ol = text_colors(c1, c2)
    txt = _initials(name)
    f, _ = fit_font(d, txt, 560, 440, 200)
    fancy_text(d, (s // 2, s // 2 + 10), txt, f, fg, ol, bold=8, outw=10)
    return img.convert("RGB")


def draw_banner(name: str, palette) -> Image.Image:
    w, h = 2560, 1440
    c1, c2 = palette[0], palette[1]
    c3 = palette[2] if len(palette) > 2 else lighten(c2, 0.4)
    img = glow(gradient((w, h), c1, c2), lighten(c2, 0.4), 0.5).convert("RGBA")
    ov = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    d = ImageDraw.Draw(ov)
    rnd = random.Random(sum(map(ord, name)))
    for _ in range(9):
        r = rnd.randint(120, 380)
        x, y = rnd.randint(-100, w + 100), rnd.randint(-100, h + 100)
        d.ellipse((x - r, y - r, x + r, y + r), fill=lighten(c3, 0.3) + (38,))
    img = Image.alpha_composite(img, ov.filter(ImageFilter.GaussianBlur(6)))
    d = ImageDraw.Draw(img)
    fg, ol = text_colors(c1, c2)
    f, size = fit_font(d, name, 1400, 220, 70)
    fancy_text(d, (w // 2, h // 2 - 10), name, f, fg, ol,
               bold=max(2, size // 40), outw=max(4, size // 18))
    d.rounded_rectangle((w // 2 - 360, h // 2 + size // 2 + 40, w // 2 + 360, h // 2 + size // 2 + 54),
                        radius=7, fill=lighten(c3, 0.2) + (255,))
    return img.convert("RGB")


# ---------- Video prevyusi ----------
def remove_bg(img: Image.Image, tol: int = 70) -> Image.Image:
    """Bir xil rangli fonni olib tashlaydi (burchaklardan boshlab). Shaffof PNG bo'lsa tegmaydi."""
    img = img.convert("RGBA")
    if img.getchannel("A").getextrema()[0] < 250:
        return img
    img.thumbnail((900, 900))
    rgb = img.convert("RGB")
    w, h = rgb.size
    marker = (255, 0, 255)
    work = rgb.copy()
    for xy in [(0, 0), (w - 1, 0), (0, h - 1), (w - 1, h - 1)]:
        if work.getpixel(xy) != marker:
            ImageDraw.floodfill(work, xy, marker, thresh=tol)
    diff = ImageChops.difference(work, Image.new("RGB", (w, h), marker))
    r, g, b = diff.split()
    mask = ImageChops.lighter(ImageChops.lighter(r, g), b).point(lambda v: 0 if v == 0 else 255)
    removed = 1 - (sum(1 for v in mask.getdata() if v) / (w * h))
    if removed < 0.05:  # fon topilmadi — rasm o'zgarishsiz qoladi
        return img
    mask = mask.filter(ImageFilter.MinFilter(3)).filter(ImageFilter.GaussianBlur(1.2))
    img.putalpha(mask)
    return img


def make_preview(paths: list, title: str) -> bytes:
    """4 qahramon rasmidan 1280x720 prevyu (YouTube thumbnail) yasaydi."""
    w, h = 1280, 720
    bg = gradient((w, h), (8, 14, 46), (0, 110, 135))
    bg = glow(bg, (40, 210, 220), 0.5).convert("RGBA")
    n = len(paths)
    col_w = w // n
    base_y = 650
    layer = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    for i, p in enumerate(paths):
        color = GLOWS[i % len(GLOWS)]
        cx = col_w * i + col_w // 2
        pad = Image.new("RGBA", (w, h), (0, 0, 0, 0))
        pd = ImageDraw.Draw(pad)
        pd.rounded_rectangle((cx - 125, base_y - 62, cx + 125, base_y + 14), radius=16,
                             fill=color + (110,), outline=(255, 255, 255, 235), width=5)
        layer = Image.alpha_composite(layer, pad.filter(ImageFilter.GaussianBlur(1.5)))
        try:
            hero = remove_bg(Image.open(p))
            box = hero.getchannel("A").getbbox()
            if box:
                hero = hero.crop(box)
            hero = ImageOps.contain(hero, (col_w - 30, 470))
            pos = (cx - hero.width // 2, base_y - 30 - hero.height)
            pad_a = Image.new("L", (w, h), 0)
            pad_a.paste(hero.getchannel("A"), pos)
            pad_a = pad_a.filter(ImageFilter.GaussianBlur(16)).point(lambda v: min(255, int(v * 1.3)))
            big = Image.new("RGBA", (w, h), color + (0,))
            big.putalpha(pad_a)
            layer = Image.alpha_composite(layer, big)
            big = Image.new("RGBA", (w, h), (0, 0, 0, 0))
            big.paste(hero, pos)
            layer = Image.alpha_composite(layer, big)
        except Exception:
            continue
    img = Image.alpha_composite(bg, layer)
    d = ImageDraw.Draw(img)
    f, size = fit_font(d, title.upper(), 1100, 130, 60)
    fancy_text(d, (w // 2, 85), title.upper(), f, (255, 255, 255), (0, 150, 190),
               bold=max(2, size // 40), outw=max(5, size // 12))
    buf = io.BytesIO()
    img.convert("RGB").save(buf, "JPEG", quality=92)
    return buf.getvalue()


def to_bytes(img: Image.Image, fmt: str) -> bytes:
    buf = io.BytesIO()
    if fmt == "JPEG":
        img.save(buf, "JPEG", quality=92)
    else:
        img.save(buf, "PNG")
    return buf.getvalue()


def make_panel(src_path: str, idx: int, w: int, h: int, out_path: str) -> str:
    """To'xtab turgan panel: neon fon ustida qahramon rasmi (fon olib tashlangan)."""
    color = GLOWS[idx % len(GLOWS)]
    dark = tuple(int(c * 0.18) for c in color)
    bg = gradient((w, h), (6, 12, 38), dark)
    bg = glow(bg, tuple(int(c * 0.55) for c in color), 0.55).convert("RGBA")
    layer = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    pad = ImageDraw.Draw(layer)
    pad.rounded_rectangle((int(w * 0.1), int(h * 0.80), int(w * 0.9), int(h * 0.88)), radius=int(w * 0.04),
                          fill=color + (120,), outline=(255, 255, 255, 235), width=max(3, w // 90))
    layer = layer.filter(ImageFilter.GaussianBlur(1.2))
    hero = remove_bg(Image.open(src_path))
    box = hero.getchannel("A").getbbox()
    if box:
        hero = hero.crop(box)
    hero = ImageOps.contain(hero, (w - int(w * 0.08), int(h * 0.66)))
    pos = ((w - hero.width) // 2, int(h * 0.84) - hero.height)
    aura = Image.new("L", (w, h), 0)
    aura.paste(hero.getchannel("A"), pos)
    aura = aura.filter(ImageFilter.GaussianBlur(max(10, w // 28))).point(lambda v: min(255, int(v * 1.3)))
    glow_layer = Image.new("RGBA", (w, h), color + (0,))
    glow_layer.putalpha(aura)
    top = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    top.paste(hero, pos)
    img = Image.alpha_composite(Image.alpha_composite(Image.alpha_composite(bg, layer), glow_layer), top)
    img.convert("RGB").save(out_path, "PNG")
    return out_path


def thumb_bytes(jpeg: bytes) -> bytes:
    """Telegram video muqovasi uchun kichik JPEG (320 px gacha)."""
    im = Image.open(io.BytesIO(jpeg)).convert("RGB")
    im.thumbnail((320, 320))
    buf = io.BytesIO()
    im.save(buf, "JPEG", quality=80)
    return buf.getvalue()
