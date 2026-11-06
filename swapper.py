"""Video joyini almashtirish: 4 panelli Tiles Hop videosida personajlar (panellar) joyini o'zgartiradi.
Yuqori qatlamlar (Subscribe paneli, 3-2-1 raqamlari kabi) o'z joyida va o'z vaqtida qoladi.
Prevyu uchun har bir panelning 'to'xtab turgan' tasviri ishlatiladi (dumaloq kesim emas)."""
import asyncio
import logging
import os
import re

import numpy as np
from PIL import Image, ImageDraw

import render
from render import FFMPEG, _run

log = logging.getLogger("swapper")

FPS = 30
SW, SH = 96, 54                 # harakatni tahlil qilish uchun kichraytirilgan kadr (har panel 24 px)
MAX_SEC = 400
DIFF_T = 40                     # "dam olish" rasmidan farq chegarasi (yuqori qatlam belgisi)


class NotSequential(Exception):
    pass


async def _probe(path: str):
    _, err = await _run(["-hide_banner", "-i", path], 60)
    dm = re.search(r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)", err)
    sm = re.search(r"Video:.*?,\s*(\d{3,5})x(\d{3,5})", err)
    if not dm or not sm:
        raise ValueError("Videoni o'qib bo'lmadi. Boshqa video yuboring.")
    h, mi, s = dm.groups()
    dur = int(h) * 3600 + int(mi) * 60 + float(s)
    return int(sm.group(1)), int(sm.group(2)), dur, "Audio:" in err


async def _motion(path: str) -> np.ndarray:
    """Har bir panelda kadrlar orasidagi harakat miqdori: (kadrlar, 4)."""
    proc = await asyncio.create_subprocess_exec(
        FFMPEG, "-v", "error", "-i", path, "-an", "-vf", f"fps={FPS},scale={SW}:{SH},format=gray",
        "-f", "rawvideo", "-", stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    out, _ = await asyncio.wait_for(proc.communicate(), 900)
    fs = SW * SH
    n = len(out) // fs
    if proc.returncode != 0 or n < FPS * 3:
        raise ValueError("Video juda qisqa yoki o'qib bo'lmadi.")
    a = np.frombuffer(out[:n * fs], dtype=np.uint8).reshape(n, SH, SW).astype(np.int16)
    d = np.abs(a[1:] - a[:-1]).astype(np.float32)
    return d.reshape(n - 1, SH, 4, SW // 4).mean(axis=(1, 3))


# ---------- Dam olish rasmi va yuqori qatlam (overlay) aniqlash ----------
async def _rest_image(video: str, w: int, dur: float, tmp: str, active: list, avoid: list, times=None):
    """Har bir panelning 'dam olish' holati: u to'xtab turgan va Subscribe ko'rinmagan paytlardagi kadrlarning
    median qiymati. times berilsa, faqat shu vaqtlardan namuna olinadi (fallback: video oxiri)."""
    if times is None:
        times = [dur * (k + 0.5) / 24 for k in range(24)]
    cw = w // 4
    crops = [[] for _ in range(4)]
    allc = [[] for _ in range(4)]
    shape = None
    fr = None
    for t in times:
        fp = os.path.join(tmp, "rf.png")
        rc, _ = await _run(["-y", "-ss", f"{t:.3f}", "-i", video, "-frames:v", "1", fp], 120)
        if rc != 0 or not os.path.exists(fp):
            continue
        fr = np.asarray(Image.open(fp).convert("RGB"))
        shape = fr.shape
        clean_t = not any(a0 - 0.2 <= t <= b0 + 0.2 for a0, b0 in avoid)
        for c in range(4):
            col = fr[:, c * cw:(c + 1) * cw].copy()
            allc[c].append(col)
            if clean_t and not (active[c][0] <= t <= active[c][1]):
                crops[c].append(col)
    if shape is None:
        return None
    r = np.zeros(shape, dtype=np.uint8)
    for c in range(4):
        use = crops[c] if len(crops[c]) >= 5 else allc[c]
        r[:, c * cw:(c + 1) * cw] = np.median(np.stack(use), axis=0).astype(np.uint8)
    if shape[1] > 4 * cw and fr is not None:
        r[:, 4 * cw:] = fr[:, 4 * cw:]
    path = os.path.join(tmp, "rest.png")
    Image.fromarray(r).save(path)
    return r, path


def _all_runs(b, min_len=1):
    out, s0 = [], None
    for i, v in enumerate(list(b) + [False]):
        if v and s0 is None:
            s0 = i
        if (not v) and s0 is not None:
            if i - s0 >= min_len:
                out.append((s0, i))
            s0 = None
    return out


def _bar_bbox(fr):
    """Kadrdagi katta oq to'rtburchak (Subscribe paneli kabi) — (x0,y0,x1,y1) yoki None.
    Barcha qator-bloklari ichidan eng keng, to'la va yassi nisbatli kandidat tanlanadi —
    shunda oq personajlar (doira kabi) adashtirmaydi."""
    hh, ww = fr.shape[:2]
    m = fr.min(axis=2) >= 215
    y0 = int(0.3 * hh)
    sub = m[y0:]
    best = None
    for r0, r1 in _all_runs(sub.mean(axis=1) > 0.14, min_len=max(int(0.05 * hh), 6)):
        if r1 - r0 > 0.3 * hh:
            continue
        band = sub[r0:r1]
        for c0, c1 in _all_runs(band.mean(axis=0) > 0.45, min_len=int(0.2 * ww)):
            box = band[:, c0:c1]
            bw, bh = c1 - c0, r1 - r0
            if bw < 2.2 * bh:
                continue
            fill = box.mean()
            if fill < 0.5:
                continue
            score = fill * bw
            if best is None or score > best[0]:
                best = (score, (c0, y0 + r0, c1, y0 + r1))
    return best[1] if best else None


def _merge(times, gap=0.35, min_len=0.4, pad_a=0.1, pad_b=0.2):
    if not times:
        return []
    out, start, prev = [], times[0], times[0]
    for t in times[1:]:
        if t - prev > gap:
            out.append((start, prev))
            start = t
        prev = t
    out.append((start, prev))
    return [(max(a - pad_a, 0.0), b + pad_b) for a, b in out if b - a >= min_len]


SWL = 384


async def _lowres(path: str, w: int, h: int, tmp: str):
    """Videoni kichik o'lchamda (10 kadr/s) diskka yozib oladi va oq Subscribe panelini aniqlaydi."""
    sh = max(2, round(SWL * h / w))
    raw_path = os.path.join(tmp, "lowres.raw")
    proc = await asyncio.create_subprocess_exec(
        FFMPEG, "-v", "error", "-i", path, "-an", "-vf", f"fps=10,scale={SWL}:{sh}",
        "-f", "rawvideo", "-pix_fmt", "rgb24", "-", stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL)
    fs = SWL * sh * 3
    bar_t, bar_boxes, n = [], [], 0
    with open(raw_path, "wb") as f:
        while True:
            try:
                data = await proc.stdout.readexactly(fs)
            except asyncio.IncompleteReadError:
                break
            f.write(data)
            bb = _bar_bbox(np.frombuffer(data, np.uint8).reshape(sh, SWL, 3))
            if bb:
                bar_t.append(n / 10)
                bar_boxes.append(bb)
            n += 1
    await proc.wait()
    bar = None
    ranges = _merge(bar_t, min_len=0.5) if len(bar_t) >= 5 else []
    if ranges:
        k = w / SWL
        x0, y0, x1, y1 = (int(np.median([b[j] for b in bar_boxes]) * k) for j in range(4))
        pad = 5
        x0, y0 = max(x0 - pad, 0), max(y0 - pad, 0)
        x1, y1 = min(x1 + pad, w), min(y1 + pad, h)
        bar = (ranges, (x0, y0, (x1 - x0) // 2 * 2, (y1 - y0) // 2 * 2))
    return {"raw": raw_path, "n": n, "sh": sh, "bar": bar}


def _erode(m):
    e = m.copy()
    e[1:-1, 1:-1] = m[1:-1, 1:-1] & m[:-2, 1:-1] & m[2:, 1:-1] & m[1:-1, :-2] & m[1:-1, 2:]
    e[0, :] = e[-1, :] = False
    e[:, 0] = e[:, -1] = False
    return e


async def _generic_ranges(low: dict, rest):
    """'Dam olish' rasmidan farq qiladigan yuqori qatlam (raqamlar, belgilar) bo'lgan vaqt oraliqlari."""
    sh, n = low["sh"], low["n"]
    fs = sh * SWL * 3
    n_real = os.path.getsize(low["raw"]) // fs
    n = min(n, n_real)
    if rest is None or n < 10:
        return None
    p0 = await asyncio.create_subprocess_exec(
        FFMPEG, "-v", "error", "-i", rest[1], "-vf", f"scale={SWL}:{sh}", "-f", "rawvideo", "-pix_fmt", "rgb24", "-",
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
    raw, _ = await p0.communicate()
    if len(raw) != SWL * sh * 3:
        return None
    rs = np.frombuffer(raw, np.uint8).reshape(sh, SWL, 3).astype(np.int16)
    mm = np.memmap(low["raw"], dtype=np.uint8, mode="r", shape=(n, sh, SWL, 3))
    cw = SWL // 4
    times = []
    for i in range(n):
        fl = _erode(np.abs(mm[i].astype(np.int16) - rs).max(axis=2) > DIFF_T)
        fr_c = [fl[:, c * cw:(c + 1) * cw].mean() for c in range(4)]
        use = [c for c in range(4) if fr_c[c] < 0.3]              # katta farq: bu panel o'ynayapti
        area = sum(fl[:, c * cw:(c + 1) * cw].sum() for c in use) / (SWL * sh)
        if len(use) >= 3 and 0.002 <= area <= 0.25:
            times.append(i / 10)
    del mm
    return _merge(times) or None


def _mask_png(path: str, bw: int, bh: int):
    m = Image.new("L", (bw, bh), 0)
    ImageDraw.Draw(m).rounded_rectangle((0, 0, bw - 1, bh - 1), radius=int(0.2 * bh), fill=255)
    m.save(path)


# ---------- O'ynash tartibi ----------
def find_segments(m: np.ndarray, strict: bool = True):
    """Ketma-ket o'ynash chegaralarini topadi: 1-panel, keyin 2-, 3-, 4-panel (kadr indekslari).
    strict=False — kuchsiz/qisqa o'ynashlarda ham urinib ko'radi."""
    spike_thr = 25 if strict else 16
    min_len = FPS if strict else int(0.6 * FPS)
    ratio = 1.8 if strict else 1.35
    own_min = 0.004 if strict else 0.0025
    n = len(m)
    p = np.vstack([np.zeros((1, 4), dtype=np.float64), np.cumsum(m, axis=0, dtype=np.float64)])
    neg = -1e18
    f = np.full((4, n + 1), neg)
    arg = np.zeros((4, n + 1), dtype=int)
    f[0] = p[:, 0]
    for c in range(1, 4):
        run = f[c - 1] - p[:, c]
        cur = 0
        for t in range(n + 1):
            if run[t] > run[cur]:
                cur = t
            arg[c, t] = cur
            f[c, t] = run[cur] + p[t, c]
    b3 = arg[3, n]
    b2 = arg[2, b3]
    b1 = arg[1, b2]
    bounds = [0, int(b1), int(b2), int(b3), n]
    for c in range(1, 4):                                  # chegarani kadr aniqligida to'g'rilash
        lo, hi = max(bounds[c] - 60, 0), min(bounds[c] + 60, n - 1)
        sc = m[lo:hi, c - 1] + m[lo:hi, c]                 # almashish kadrida ikki panel ham keskin o'zgaradi
        t = int(np.argmax(sc))
        if sc[t] > spike_thr and m[lo + t, c - 1] > 10 and m[lo + t, c] > 10:
            bounds[c] = lo + t + 1
    if not (0 < bounds[1] < bounds[2] < bounds[3] < n):
        raise NotSequential("panel chegaralari topilmadi")
    for c in range(4):
        a, b = bounds[c], bounds[c + 1]
        if b - a < min_len:
            raise NotSequential(f"{c + 1}-panel oynasi juda qisqa ({(b - a) / FPS:.1f} s)")
        own = m[a:b, c].mean()
        others = np.delete(m[a:b], c, axis=1).mean()
        if own < ratio * others or own < own_min:
            raise NotSequential(f"{c + 1}-panel farqi yetarli emas (own={own:.4f})")
    return bounds


def trim_edges(m: np.ndarray, bounds: list, spike_thr: int = 25):
    """Boshidagi (hamma panel to'xtab, 3-2-1 sanash) va oxiridagi qismlarni topadi: (i0, o0) kadr indekslari."""
    def spike(c, a, b, first):
        seg = m[a:b, c]
        n = len(seg)
        lo, hi = (0, int(n * 0.7)) if first else (int(n * 0.3), n)
        if hi - lo < 5:
            return None
        k = lo + int(np.argmax(seg[lo:hi]))
        return a + k + 1 if seg[k] > spike_thr else None

    def sustained(c, a, b, first):
        seg = m[a:b, c]
        if len(seg) < 30:
            return a if first else b
        med = np.median(np.lib.stride_tricks.sliding_window_view(seg, 15), axis=1)
        idx = np.nonzero(med > 0.3 * np.percentile(seg, 75))[0]
        if not len(idx):
            return a if first else b
        return a + int(idx[0]) + 7 if first else a + int(idx[-1]) + 8

    i0 = spike(0, bounds[0], bounds[1], True)
    if i0 is None:
        i0 = sustained(0, bounds[0], bounds[1], True)
    o0 = spike(3, bounds[3], bounds[4], False)
    if o0 is None:
        o0 = sustained(3, bounds[3], bounds[4], False)
    return i0, min(o0, bounds[4])


# ---------- ffmpeg ----------
async def _encode(args_in, filt, out, vb, audio):
    args = ["-y"] + args_in + ["-filter_complex", filt, "-map", "[v]"]
    if audio:
        args += ["-map", "0:a:0?", "-af", "aresample=44100,aformat=channel_layouts=stereo",
                 "-c:a", "aac", "-b:a", "128k"]
    args += ["-c:v", "libx264", "-preset", "veryfast", "-threads", "2", "-r", str(FPS),
             "-b:v", f"{vb}k", "-maxrate", f"{int(vb * 1.4)}k", "-bufsize", f"{vb * 2}k",
             "-g", "60", "-keyint_min", "60", "-sc_threshold", "0", "-pix_fmt", "yuv420p", out]
    rc, err = await _run(args)
    if rc != 0:
        raise RuntimeError("ffmpeg xatosi: " + err[-500:])


def _local(ranges, t0, d):
    out = []
    for a, b in ranges:
        lo, hi = max(a, t0), min(b, t0 + d)
        if hi > lo:
            out.append((lo - t0, hi - t0))
    return out


def _en(ranges):
    return "+".join(f"between(t,{a:.3f},{b:.3f})" for a, b in ranges)


def _diff_mask(src_label, rest_label, boxes, cw, tag):
    """Asl kadr va 'dam olish' rasmi farqidan yuqori qatlam maskasi (o'ynayotgan panel chiqarib tashlanadi)."""
    parts = [f"[{src_label}][{rest_label}]blend=all_mode=difference[{tag}0]"]
    last = f"{tag}0"
    for j, (px, a, b) in enumerate(boxes):
        parts.append(f"[{last}]drawbox=x={px}:y=0:w={cw}:h=ih:color=black:t=fill:"
                     f"enable='between(t,{a:.3f},{b:.3f})'[{tag}{j + 1}]")
        last = f"{tag}{j + 1}"
    parts.append(f"[{last}]format=gray,lut=y='if(gt(val,{DIFF_T - 12}),255,0)',erosion,dilation,dilation,dilation[{tag}m]")
    return parts, f"{tag}m"


def _filter(order, cw, ph, src_i=None, bar=None, bar_i=None, gen=None, clean=None, rest_i=None):
    """Panellar tartibi o'zgaradi.
    clean = (asl vaqt oraliqlari, qutilar): asl kadrdagi yuqori qatlam 'dam olish' rasmi bilan tozalanadi
            (aks holda u panel bilan birga siljib ketadi);
    gen   = (chiqish vaqti oraliqlari, qutilar): yuqori qatlam o'z joyiga o'sha vaqtdan qaytariladi (src_i — asl video);
    bar   = (oraliqlar, (x,y,eni,bo'yi)): oq Subscribe paneli."""
    parts = []
    n_rest = (2 if clean else 0) + (1 if gen else 0)
    labels = [f"rs{k}" for k in range(n_rest)]
    if n_rest:
        parts.append(f"[{rest_i}:v]format=rgb24,split={n_rest}" + "".join(f"[{x}]" for x in labels)
                     if n_rest > 1 else f"[{rest_i}:v]format=rgb24[rs0]")
    content = "0:v"
    if clean:
        ranges, boxes = clean
        parts.append("[0:v]format=rgb24,split[k1][k2]")
        pp, ml = _diff_mask("k2", labels.pop(0), boxes, cw, "kd")
        parts += pp
        parts.append(f"[{labels.pop(0)}]format=rgba[rp]")
        parts.append(f"[rp][{ml}]alphamerge[rpatch]")
        parts.append(f"[k1][rpatch]overlay=0:0:enable='{_en(ranges)}'[cv]")
        content = "cv"
    parts.append(f"[{content}]split=4" + "".join(f"[a{i}]" for i in range(4)))
    for c, old in enumerate(order):
        parts.append(f"[a{c}]crop={cw}:{ph}:{old * cw}:0[c{c}]")
    parts.append("".join(f"[c{c}]" for c in range(4)) + "hstack=inputs=4[base]")
    cur = "base"
    n_br = (1 if bar else 0) + (1 if gen else 0)
    if n_br:
        parts.append(f"[{src_i}:v]split={n_br}" + "".join(f"[s{k}]" for k in range(n_br))
                     if n_br > 1 else f"[{src_i}:v]null[s0]")
    k = 0
    if bar:
        ranges, (x, y, bw, bh) = bar
        parts.append(f"[s{k}]crop={bw}:{bh}:{x}:{y},format=rgba[bc]")
        parts.append(f"[{bar_i}:v]format=gray[bm]")
        parts.append("[bc][bm]alphamerge[bova]")
        parts.append(f"[{cur}][bova]overlay={x}:{y}:enable='{_en(ranges)}'[o1]")
        cur = "o1"
        k += 1
    if gen:
        ranges, boxes = gen
        parts.append(f"[s{k}]format=rgb24,split[g1][g2]")
        pp, ml = _diff_mask("g2", labels.pop(0), boxes, cw, "gd")
        parts += pp
        parts.append("[g1]format=rgba[gc]")
        parts.append(f"[gc][{ml}]alphamerge[gova]")
        parts.append(f"[{cur}][gova]overlay=0:0:enable='{_en(ranges)}'[o2]")
        cur = "o2"
    parts.append(f"[{cur}]null[v]")
    return ";".join(parts)


def _rest_panels(rest_arr, order, cw, ph, tmp):
    """Prevyu uchun: har bir YANGI o'ringa mos panelning 'dam olish' tasvirini kesib saqlaydi."""
    paths = []
    for j, old in enumerate(order):
        col = Image.fromarray(rest_arr[:, old * cw:(old + 1) * cw].copy()).resize((cw, ph), Image.LANCZOS)
        p = os.path.join(tmp, f"rp{j}.png")
        col.save(p)
        paths.append(p)
    return paths


async def build(video: str, order: list, out: str, tmp: str, progress=None):
    """order[j] = yangi j-o'rindagi panelning eski indeksi (0..3).
    Qaytaradi: {"total": soniya, "ordered": ketma-ketmi, "panels": [prevyu uchun 4 ta rasm],
                "diag": {...tahlil natijalari}}"""
    w, h, dur, has_audio = await _probe(video)
    if dur > MAX_SEC:
        raise ValueError(f"Video juda uzun (eng ko'pi {MAX_SEC // 60} daqiqa).")
    cw = (w // 4) // 2 * 2
    ph = h // 2 * 2
    vb = int(min(5000, max(300, render.TARGET_MB * 8192 / dur - 128)))
    diag = {"sequential": True, "bar": None, "gen": None, "reason": ""}
    if progress:
        await progress("🔎 Panellar, Subscribe va raqamlar (yuqori qatlam) aniqlanmoqda...")
    low_res, mo_res = await asyncio.gather(_lowres(video, w, h, tmp), _motion(video), return_exceptions=True)
    if isinstance(mo_res, Exception):
        raise mo_res
    bar_info = None if isinstance(low_res, Exception) else low_res["bar"]
    diag["bar"] = bar_info[0] if bar_info else None
    bar_mask = os.path.join(tmp, "ovmask.png")
    if bar_info:
        _mask_png(bar_mask, bar_info[1][2], bar_info[1][3])
    sequential = True
    bounds = None
    try:
        bounds = find_segments(mo_res, strict=True)
    except NotSequential as e:
        try:
            bounds = find_segments(mo_res, strict=False)
            diag["reason"] = f"kuchsiz signal ({e})"
        except NotSequential as e2:
            sequential = False
            diag["sequential"] = False
            diag["reason"] = str(e2)
    log.info("swap tahlil: sequential=%s reason=%s bar=%s", sequential, diag["reason"], diag["bar"])
    rest, gen_ranges = None, None
    if sequential and not isinstance(low_res, Exception):
        try:
            i0t, o0t = trim_edges(mo_res, bounds)
            ftt = [b / FPS for b in bounds]
            ftt[0], ftt[4] = i0t / FPS, o0t / FPS
            active = [(ftt[c], ftt[c + 1]) for c in range(4)]
            avoid = bar_info[0] if bar_info else []
            rest = await _rest_image(video, w, dur, tmp, active, avoid)
            gen_ranges = await _generic_ranges(low_res, rest)
        except Exception:
            log.exception("yuqori qatlam tahlili xatosi")
            rest, gen_ranges = None, None
    diag["gen"] = gen_ranges
    log.info("swap tahlil: gen=%s", gen_ranges)
    scan = {"bar": bar_info, "gen": gen_ranges}

    def build_inputs(content_args, s0, t0, d, gen_boxes):
        """Kirishlar va filtr. s0 — asl videodagi boshlanish, t0 — tayyor videodagi boshlanish."""
        bar_lr = _local(scan["bar"][0], t0, d) if scan["bar"] else []
        use_gen = bool(scan["gen"] and rest is not None and gen_boxes is not None)
        gen_lr = _local(scan["gen"], t0, d) if use_gen else []
        clean_lr = _local(scan["gen"], s0, d) if use_gen else []
        if not (bar_lr or gen_lr or clean_lr):
            return content_args, _filter(order, cw, ph)
        args = list(content_args)
        idx = 1
        src_i = bar_i = rest_i = None
        if bar_lr or gen_lr:
            args += ["-ss", f"{t0:.3f}", "-t", f"{d:.3f}", "-i", video]
            src_i, idx = idx, idx + 1
        if bar_lr:
            args += ["-loop", "1", "-framerate", str(FPS), "-t", f"{d:.3f}", "-i", bar_mask]
            bar_i, idx = idx, idx + 1
        if gen_lr or clean_lr:
            args += ["-loop", "1", "-framerate", str(FPS), "-t", f"{d:.3f}", "-i", rest[1]]
            rest_i, idx = idx, idx + 1

        def boxes_at(t_start):
            return [(c * cw, max(a - t_start, 0.0), min(b - t_start, d)) for c, a, b in gen_boxes
                    if b - t_start > 0 and a - t_start < d]
        flt = _filter(order, cw, ph, src_i=src_i,
                      bar=(bar_lr, scan["bar"][1]) if bar_lr else None, bar_i=bar_i,
                      gen=(gen_lr, boxes_at(t0)) if gen_lr else None,
                      clean=(clean_lr, boxes_at(s0)) if clean_lr else None, rest_i=rest_i)
        return args, flt

    if not sequential:
        if progress:
            await progress("🎬 Panellar joyi almashtirilmoqda (yuqori qatlam saqlanmadi, "
                           "videoda ketma-ket o'ynash topilmadi)...")
        args, flt = build_inputs(["-i", video], 0.0, 0.0, dur, None)
        await _encode(args, flt, out, vb, has_audio)
        times = [max(dur - 1.5, 0) + i * 0.2 for i in range(6)]
        r2 = await _rest_image(video, w, dur, tmp, [(0, 0)] * 4,
                               bar_info[0] if bar_info else [], times=times)
        panels = _rest_panels(r2[0], order, cw, ph, tmp) if r2 else []
        return {"total": dur, "ordered": False, "panels": panels, "diag": diag}

    i0, o0 = trim_edges(mo_res, bounds)
    ft = [b / FPS for b in bounds]
    ft[0], ft[4] = i0 / FPS, o0 / FPS                       # o'ynash boshlanishi va tugashi
    margin = 0.15
    gen_boxes = [(c, ft[c] - margin, ft[c + 1] + margin) for c in range(4)]
    plan = []                                              # (nom, manba boshi, manba oxiri)
    if i0 / FPS >= 0.5:
        plan.append(("intro", 0.0, i0 / FPS))
    for j, old in enumerate(order):
        plan.append((f"play{j}", ft[old], ft[old + 1]))
    if dur - o0 / FPS >= 0.5:
        plan.append(("outro", o0 / FPS, dur))
    segs, t0, play_mid = [], 0.0, []
    for k, (name, s0, e0) in enumerate(plan):
        if progress:
            await progress(f"🎬 {k + 1}-qism tayyorlanmoqda ({k + 1}/{len(plan)})...")
        d = e0 - s0
        seg = os.path.join(tmp, f"sw{k}.mp4")
        args, flt = build_inputs(["-ss", f"{s0:.3f}", "-t", f"{d:.3f}", "-i", video], s0, t0, d, gen_boxes)
        await _encode(args, flt, seg, vb, has_audio)
        segs.append(seg)
        if name.startswith("play"):
            play_mid.append(t0 + d / 2)
        t0 += d
    if progress:
        await progress("🔗 Qismlar birlashtirilmoqda...")
    lst = os.path.join(tmp, "swlist.txt")
    with open(lst, "w") as f:
        f.write("".join(f"file '{x}'\n" for x in segs))
    rc, err = await _run(["-y", "-f", "concat", "-safe", "0", "-i", lst, "-c", "copy",
                          "-movflags", "+faststart", out])
    if rc != 0:
        raise RuntimeError("ffmpeg birlashtirish xatosi: " + err[-500:])
    if os.path.getsize(out) > 49 * 1024 * 1024:
        raise RuntimeError("Video hajmi 50 MB dan oshib ketdi")
    panels = _rest_panels(rest[0], order, cw, ph, tmp) if rest is not None else []
    return {"total": dur, "ordered": True, "panels": panels, "diag": diag}
