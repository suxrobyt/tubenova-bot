"""Rasm yaratish: logo, banner (AI rasm + zaxira dizayn) va video prevyusi."""
import asyncio
import colorsys
import io
import os
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
async def fetch_image(prompt: str, width: int, height: int, wm: int = 0) -> Image.Image:
    """Matn tavsifi bo'yicha AI rasm yaratadi.
    POLLINATIONS_KEY bo'lsa suv belgisiz yangi API ishlatiladi. Kalit bo'lmasa suv belgisi pastki burchakda
    chiqadi: wm > 0 bo'lsa, rasm kattaroq so'raladi va tepa-past chetlari kesib tashlanadi."""
    key = os.getenv("POLLINATIONS_KEY", "")
    base = "https://gen.pollinations.ai/image/" if key else API
    headers = {"Authorization": f"Bearer {key}"} if key else {}
    pad = 0 if key else wm
    last = "noma'lum xato"
    async with httpx.AsyncClient(timeout=90, follow_redirects=True, headers=headers) as client:
        for attempt in range(3):
            url = (base + urllib.parse.quote(prompt[:1400]) +
                   f"?width={width}&height={height + 2 * pad}&seed={random.randint(1, 999999)}"
                   "&nologo=true&model=flux")
            try:
                r = await client.get(url)
                if r.status_code == 200 and r.headers.get("content-type", "").startswith("image"):
                    img = Image.open(io.BytesIO(r.content)).convert("RGB")
                    if pad:
                        sy = img.height / (height + 2 * pad)
                        box = (0, int(pad * sy), img.width, int((pad + height) * sy))
                        img = img.crop(box).resize((width, height), Image.LANCZOS)
                    return img
                last = f"HTTP {r.status_code}"
            except Exception as e:
                last = repr(e)
            await asyncio.sleep(6 * (attempt + 1))
    raise RuntimeError(f"Rasm xizmati javob bermadi: {last}")


# ---------- Ranglar nomi (AI uchun) ----------
HEX_NAME = {}
for _w, _h in COLOR_WORDS.items():
    if _w.isascii() and _h not in HEX_NAME:
        HEX_NAME[_h] = _w
HEX_NAME.update({"#ffffff": "white", "#111111": "black", "#9e9e9e": "gray", "#e53935": "red",
                 "#1e88e5": "blue", "#2e9e47": "green", "#fdd835": "yellow", "#fb8c00": "orange",
                 "#8e24aa": "purple", "#ec407a": "pink", "#ffc107": "gold", "#00bcd4": "cyan"})


def color_names(hexes: list) -> list:
    return [HEX_NAME.get(h.lower(), h) for h in hexes]


def names_to_hex(names) -> list:
    out = []
    for n in names if isinstance(names, list) else []:
        n = str(n).strip().lower()
        if _valid(n):
            out.append(n)
            continue
        for word, hv in COLOR_WORDS.items():
            if word.isascii() and (n == word or n.startswith(word) or word in n.split()):
                if hv not in out:
                    out.append(hv)
                break
    return out


def pick_accent(palette) -> tuple:
    """Eng yorqin (to'yingan) rang; hammasi oq/qora/kulrang bo'lsa, eng yorug'i."""
    best, score = palette[0], -1.0
    for c in palette:
        h, s, v = colorsys.rgb_to_hsv(*(x / 255 for x in c))
        sc = s * v if v > 0.25 else 0
        if sc > score:
            best, score = c, sc
    if score < 0.2:
        best = max(palette, key=lambda c: sum(c))
    return best


def _title_font(size: int):
    for name in ("DejaVuSans-BoldOblique.ttf", "DejaVuSans-Bold.ttf", "LiberationSans-BoldItalic.ttf",
                 "LiberationSans-Bold.ttf", "Arial Bold.ttf", "arialbd.ttf"):
        try:
            return ImageFont.truetype(name, size)
        except Exception:
            pass
    return _font(size)


def chrome_title(text: str, accent, max_w: int, max_h: int, start: int = 260) -> Image.Image:
    """Metall (kumush) + rangli 3D yozuv: birinchi qism kumush, qolgani accent rangda."""
    probe = ImageDraw.Draw(Image.new("L", (10, 10)))
    size = start
    f = _title_font(size)
    while size > 50 and (probe.textlength(text, font=f) > max_w or size * 1.25 > max_h):
        size -= 8
        f = _title_font(size)
    words = text.split()
    if len(words) >= 2:
        a, b = words[0] + " ", " ".join(words[1:])
    else:
        k = max(1, int(len(text) * 0.45)) if len(text) > 1 else 1
        a, b = text[:k], text[k:]
    wa = probe.textlength(a, font=f)
    wb = probe.textlength(b, font=f) if b else 0
    pad = size // 3
    w, h = int(wa + wb) + 2 * pad, int(size * 1.45) + 2 * pad

    def mask_for(txt, x):
        m = Image.new("L", (w, h), 0)
        if txt:
            ImageDraw.Draw(m).text((x, pad), txt, font=f, fill=255)
        return m

    ma, mb = mask_for(a, pad), mask_for(b, pad + wa)
    full = ImageChops.lighter(ma, mb)
    sw = max(3, size // 26)
    outline = full.filter(ImageFilter.MaxFilter(2 * sw + 1))
    layer = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    glow_a = full.filter(ImageFilter.GaussianBlur(size // 9)).point(lambda v: int(v * 0.75))
    gl = Image.new("RGBA", (w, h), tuple(accent) + (0,))
    gl.putalpha(glow_a)
    layer = Image.alpha_composite(layer, gl)
    shadow = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    shadow.putalpha(ImageChops.offset(outline, sw, sw).filter(ImageFilter.GaussianBlur(sw)))
    layer = Image.alpha_composite(layer, shadow)
    dark = Image.new("RGBA", (w, h), (8, 8, 12, 255))
    dark.putalpha(outline)
    layer = Image.alpha_composite(layer, dark)
    grad = Image.linear_gradient("L").resize((w, h))
    vshape = ImageChops.difference(grad, Image.new("L", (w, h), 120)).point(lambda v: min(255, v * 2))
    silver = ImageOps.colorize(vshape, black=(112, 118, 130), mid=(176, 182, 194),
                               white=(252, 252, 255)).convert("RGBA")
    silver.putalpha(ma)
    layer = Image.alpha_composite(layer, silver)
    acc = ImageOps.colorize(vshape, black=tuple(int(c * 0.4) for c in accent), mid=tuple(accent),
                            white=lighten(accent, 0.5)).convert("RGBA")
    acc.putalpha(mb)
    layer = Image.alpha_composite(layer, acc)
    box = layer.getchannel("A").getbbox()
    return layer.crop(box) if box else layer


def compose_banner(art: Image.Image, title: str, palette) -> Image.Image:
    """2560x1440 banner: markazda 2560x423 rasm lentasi, ustida 3D nom; chetlari qorong'i (TV uchun)."""
    w, h, bh = 2560, 1440, 423
    bg = gradient((w, h), (18, 20, 26), (9, 10, 14))
    ar = w / bh
    aw, ah = art.size
    if aw / ah > ar:
        nw = int(ah * ar)
        box = ((aw - nw) // 2, 0, (aw - nw) // 2 + nw, ah)
    else:
        nh = int(aw / ar)
        box = (0, (ah - nh) // 2, aw, (ah - nh) // 2 + nh)
    band = art.convert("RGB").crop(box).resize((w, bh), Image.LANCZOS)
    y = (h - bh) // 2
    bg.paste(band, (0, y))
    if title:
        t = chrome_title(title, pick_accent(palette), 1400, int(bh * 0.78))
        bg.paste(t, ((w - t.width) // 2, y + (bh - t.height) // 2), t)
    return bg


# ---------- Zaxira dizayn (AI xizmati ishlamasa ham rasm chiqadi) ----------
def _initial(name: str) -> str:
    words = re.findall(r"\w+", name, re.U)
    return words[0][0].upper() if words else "T"


def draw_logo(name: str, palette) -> Image.Image:
    """Zaxira logo: qora doira, yorqin halqa va metall bosh harf."""
    s = 1024
    acc = pick_accent(palette)
    img = Image.new("RGB", (s, s), (6, 6, 8))
    img = glow(img, tuple(int(c * 0.35) for c in acc), 0.5).convert("RGBA")
    ring = Image.new("RGBA", (s, s), (0, 0, 0, 0))
    ImageDraw.Draw(ring).ellipse((70, 70, s - 70, s - 70), outline=tuple(acc) + (255,), width=26)
    img = Image.alpha_composite(img, ring.filter(ImageFilter.GaussianBlur(18)))
    img = Image.alpha_composite(img, ring)
    ImageDraw.Draw(img).ellipse((120, 120, s - 120, s - 120), outline=(255, 255, 255, 200), width=5)
    t = chrome_title(_initial(name), acc, 560, 560, start=520)
    img.paste(t, ((s - t.width) // 2, (s - t.height) // 2), t)
    return img.convert("RGB")


def procedural_art(palette, w: int = 2560, h: int = 423) -> Image.Image:
    """AI rasm bo'lmasa: qorong'i fon, rangli yorug'lik izlari va zarralar."""
    c1 = pick_accent(palette)
    c2 = palette[1] if len(palette) > 1 else lighten(c1, 0.5)
    img = gradient((w, h), (14, 15, 20), (8, 9, 12)).convert("RGBA")
    rnd = random.Random(sum(c1) + sum(c2))
    layer = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    d = ImageDraw.Draw(layer)
    for side in (0, 1):
        col = c1 if side == 0 else c2
        for _ in range(14):
            x = rnd.randint(0, w // 3) if side == 0 else rnd.randint(2 * w // 3, w)
            y = rnd.randint(-50, h)
            ln, wd = rnd.randint(120, 520), rnd.randint(3, 10)
            d.polygon([(x, y), (x + wd, y), (x + wd + ln // 3, y + ln), (x + ln // 3, y + ln)],
                      fill=tuple(col) + (rnd.randint(40, 120),))
        for _ in range(26):
            x = rnd.randint(0, w // 3) if side == 0 else rnd.randint(2 * w // 3, w)
            y = rnd.randint(0, h)
            r = rnd.randint(3, 9)
            d.polygon([(x, y - r), (x + r, y), (x, y + r), (x - r, y)], fill=tuple(lighten(col, 0.5)) + (200,))
        cx = w // 6 if side == 0 else 5 * w // 6
        d.ellipse((cx - 150, h // 2 - 95, cx + 150, h // 2 + 95), fill=tuple(col) + (150,))
    img = Image.alpha_composite(img, layer.filter(ImageFilter.GaussianBlur(14)))
    img = Image.alpha_composite(img, layer)
    return img.convert("RGB")


def draw_banner(name: str, palette) -> Image.Image:
    return compose_banner(procedural_art(palette), name, palette)


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


def oval_cutout(img: Image.Image) -> Image.Image:
    """Panel tepasidagi oval personaj kesimini shaffof oval qilib beradi."""
    img = img.convert("RGBA")
    m = Image.new("L", img.size, 0)
    ImageDraw.Draw(m).ellipse((2, 2, img.width - 3, img.height - 3), fill=255)
    img.putalpha(m.filter(ImageFilter.GaussianBlur(1)))
    return img


def _compose_preview(heroes: list, title: str) -> bytes:
    """Qahramonlar (RGBA) dan 1280x720 prevyu (YouTube thumbnail) yasaydi."""
    w, h = 1280, 720
    bg = gradient((w, h), (8, 14, 46), (0, 110, 135))
    bg = glow(bg, (40, 210, 220), 0.5).convert("RGBA")
    n = len(heroes)
    col_w = w // n
    base_y = 650
    layer = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    for i, hero in enumerate(heroes):
        color = GLOWS[i % len(GLOWS)]
        cx = col_w * i + col_w // 2
        pad = Image.new("RGBA", (w, h), (0, 0, 0, 0))
        ImageDraw.Draw(pad).rounded_rectangle((cx - 125, base_y - 62, cx + 125, base_y + 14), radius=16,
                                              fill=color + (110,), outline=(255, 255, 255, 235), width=5)
        layer = Image.alpha_composite(layer, pad.filter(ImageFilter.GaussianBlur(1.5)))
        try:
            box = hero.getchannel("A").getbbox()
            if box:
                hero = hero.crop(box)
            hero = ImageOps.contain(hero, (col_w - 14, 470))
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


def make_preview(paths: list, title: str) -> bytes:
    """Yuborilgan rasmlar (fon olib tashlanadi) dan prevyu."""
    heroes = []
    for p in paths:
        try:
            heroes.append(remove_bg(Image.open(p)))
        except Exception:
            heroes.append(Image.new("RGBA", (10, 10), (0, 0, 0, 0)))
    return _compose_preview(heroes, title)


def make_preview_imgs(images: list, title: str) -> bytes:
    """Tayyor RGBA personaj rasmlaridan (video kadrlaridan olingan) prevyu."""
    return _compose_preview(images, title)


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
