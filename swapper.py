"""Video joyini almashtirish: 4 panelli Tiles Hop videosida personajlar joyini o'zgartiradi."""
import asyncio
import os
import re

import numpy as np
from PIL import Image, ImageDraw

import render
from render import FFMPEG, _run

FPS = 30
SW, SH = 96, 54                 # tahlil uchun kichraytirilgan kadr (har panel 24 px)
MAX_SEC = 400


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


def _longest_run(b):
    best, s0 = None, None
    for i, v in enumerate(list(b) + [False]):
        if v and s0 is None:
            s0 = i
        if (not v) and s0 is not None:
            if best is None or i - s0 > best[1] - best[0]:
                best = (s0, i)
            s0 = None
    return best


def _bar_bbox(fr):
    """Kadrdagi katta oq to'rtburchak (Subscribe paneli kabi) — (x0,y0,x1,y1) yoki None."""
    hh, ww = fr.shape[:2]
    m = fr.min(axis=2) >= 215
    y0 = int(0.45 * hh)
    sub = m[y0:]
    rows = _longest_run(sub.mean(axis=1) > 0.14)
    if not rows or rows[1] - rows[0] < 0.06 * hh:
        return None
    band = sub[rows[0]:rows[1]]
    cols = _longest_run(band.mean(axis=0) > 0.45)
    if not cols or cols[1] - cols[0] < 0.2 * ww:
        return None
    box = band[:, cols[0]:cols[1]]
    bw, bh = cols[1] - cols[0], rows[1] - rows[0]
    if bw < 2.2 * bh or box.mean() < 0.6:
        return None
    return cols[0], y0 + rows[0], cols[1], y0 + rows[1]


async def _overlay(path: str, w: int, h: int):
    """Videoga qo'yilgan oq «Subscribe» paneli: ([(boshlanish, tugash)], (x, y, eni, bo'yi)) yoki None."""
    sw = 480
    sh = max(2, round(sw * h / w))
    proc = await asyncio.create_subprocess_exec(
        FFMPEG, "-v", "error", "-i", path, "-an", "-vf", f"fps=10,scale={sw}:{sh}",
        "-f", "rawvideo", "-pix_fmt", "rgb24", "-", stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL)
    fs = sw * sh * 3
    flagged, boxes, i = [], [], 0
    while True:
        try:
            data = await proc.stdout.readexactly(fs)
        except asyncio.IncompleteReadError:
            break
        bb = _bar_bbox(np.frombuffer(data, np.uint8).reshape(sh, sw, 3))
        if bb:
            flagged.append(i / 10)
            boxes.append(bb)
        i += 1
    await proc.wait()
    if len(flagged) < 5:
        return None
    ranges, start, prev = [], flagged[0], flagged[0]
    for t in flagged[1:]:
        if t - prev > 0.35:
            ranges.append((start, prev))
            start = t
        prev = t
    ranges.append((start, prev))
    ranges = [(max(a - 0.1, 0.0), b + 0.2) for a, b in ranges if b - a >= 0.5]
    if not ranges:
        return None
    k = w / sw
    x0, y0, x1, y1 = (int(np.median([b[j] for b in boxes]) * k) for j in range(4))
    pad = 5
    x0, y0 = max(x0 - pad, 0), max(y0 - pad, 0)
    x1, y1 = min(x1 + pad, w), min(y1 + pad, h)
    bw, bh = (x1 - x0) // 2 * 2, (y1 - y0) // 2 * 2
    return ranges, (x0, y0, bw, bh)


def _mask_png(path: str, bw: int, bh: int):
    m = Image.new("L", (bw, bh), 0)
    ImageDraw.Draw(m).rounded_rectangle((0, 0, bw - 1, bh - 1), radius=int(0.2 * bh), fill=255)
    m.save(path)


def find_segments(m: np.ndarray):
    """Ketma-ket o'ynash chegaralarini topadi: 1-panel, keyin 2-, 3-, 4-panel (kadr indekslari)."""
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
        if sc[t] > 25 and m[lo + t, c - 1] > 10 and m[lo + t, c] > 10:
            bounds[c] = lo + t + 1
    if not (0 < bounds[1] < bounds[2] < bounds[3] < n):
        raise NotSequential()
    for c in range(4):
        a, b = bounds[c], bounds[c + 1]
        if b - a < FPS:                                   # 1 soniyadan qisqa bo'lak
            raise NotSequential()
        own = m[a:b, c].mean()
        others = np.delete(m[a:b], c, axis=1).mean()
        if own < 1.8 * others or own < 0.004:             # shu panel boshqalardan aniq faol emas
            raise NotSequential()
    return bounds


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


def _filter(order, cw, h, ov=None):
    """ov = (mahalliy_oraliqlar, (x, y, eni, bo'yi)): Subscribe paneli joyida qoladi (1-kirish: asl video, 2-kirish: maska)."""
    parts = ["[0:v]split=4" + "".join(f"[a{i}]" for i in range(4))]
    for c, old in enumerate(order):
        parts.append(f"[a{c}]crop={cw}:{h}:{old * cw}:0[c{c}]")
    last = "v" if not ov else "base"
    parts.append("".join(f"[c{c}]" for c in range(4)) + f"hstack=inputs=4[{last}]")
    if ov:
        ranges, (x, y, bw, bh) = ov
        en = "+".join(f"between(t,{a:.3f},{b:.3f})" for a, b in ranges)
        parts.append(f"[1:v]crop={bw}:{bh}:{x}:{y},format=rgba[ovc]")
        parts.append("[2:v]format=gray[msk]")
        parts.append("[ovc][msk]alphamerge[ova]")
        parts.append(f"[base][ova]overlay={x}:{y}:enable='{en}'[v]")
    return ";".join(parts)


def _local_ranges(ranges, t0, d):
    out = []
    for a, b in ranges:
        lo, hi = max(a, t0), min(b, t0 + d)
        if hi - lo > 0.05:
            out.append((lo - t0, hi - t0))
    return out


async def _frame(path: str, t: float, out: str):
    await _run(["-y", "-ss", f"{t:.3f}", "-i", path, "-frames:v", "1", out])


def _local_ranges(ranges, t0, d):
    out = []
    for a, b in ranges:
        lo, hi = max(a, t0), min(b, t0 + d)
        if hi > lo:
            out.append((lo - t0, hi - t0))
    return out


async def _frames(video: str, times: list, order: list, cw: int, ph: int, tmp: str):
    """Tayyor videodan har bir panelning tepa qismini (oval personaj) kesib oladi."""
    paths = []
    for j, t in enumerate(times):
        fr = os.path.join(tmp, f"pf{j}.png")
        crop = f"crop={cw}:{int(0.4 * ph) // 2 * 2}:{j * cw}:0"
        rc, _ = await _run(["-y", "-ss", f"{t:.3f}", "-i", video, "-frames:v", "1", "-vf", crop, fr], 120)
        if rc == 0 and os.path.exists(fr):
            paths.append(fr)
    return paths


async def build(video: str, order: list, out: str, tmp: str, progress=None):
    """order[j] = yangi j-o'rindagi panelning eski indeksi (0..3).
    Qaytaradi: {"total": soniya, "ordered": ketma-ketmi, "frames": [prevyu uchun oval kesimlar]}"""
    w, h, dur, has_audio = await _probe(video)
    if dur > MAX_SEC:
        raise ValueError(f"Video juda uzun (eng ko'pi {MAX_SEC // 60} daqiqa).")
    cw = (w // 4) // 2 * 2
    ph = h // 2 * 2
    vb = int(min(5000, max(300, render.TARGET_MB * 8192 / dur - 128)))
    if progress:
        await progress("🔎 Panellar, Subscribe tugmalari va o'ynash tartibi aniqlanmoqda...")
    ov_res, mo_res = await asyncio.gather(_overlay(video, w, h), _motion(video), return_exceptions=True)
    ov = None if isinstance(ov_res, Exception) else ov_res
    if isinstance(mo_res, Exception):
        raise mo_res
    mask = os.path.join(tmp, "ovmask.png")
    if ov:
        _mask_png(mask, ov[1][2], ov[1][3])
    sequential = True
    try:
        bounds = find_segments(mo_res)
    except NotSequential:
        sequential = False

    def inputs(main_args, t0, d):
        """(kirishlar, filtr uchun overlay) — overlay faqat shu qismga to'g'ri kelsa."""
        lr = _local_ranges(ov[0], t0, d) if ov else []
        if not lr:
            return main_args, _filter(order, cw, ph)
        args = main_args + ["-ss", f"{t0:.3f}", "-t", f"{d:.3f}", "-i", video,
                            "-loop", "1", "-framerate", str(FPS), "-t", f"{d:.3f}", "-i", mask]
        return args, _filter(order, cw, ph, (lr, ov[1]))

    if not sequential:
        if progress:
            await progress("🎬 Panellar joyi almashtirilmoqda...")
        args, flt = inputs(["-i", video], 0.0, dur)
        await _encode(args, flt, out, vb, has_audio)
        times = [dur * (j + 0.5) / 4 for j in range(4)]
        return {"total": dur, "ordered": False, "frames": await _frames(out, times, order, cw, ph, tmp)}
    times_src = [b / FPS for b in bounds]
    times_src[-1] = dur
    segs, starts, t0 = [], [], 0.0
    for j, old in enumerate(order):
        if progress:
            await progress(f"🎬 {j + 1}-qism tayyorlanmoqda ({j + 1}/4)...")
        s0, e0 = times_src[old], times_src[old + 1]
        d = e0 - s0
        seg = os.path.join(tmp, f"sw{j}.mp4")
        args, flt = inputs(["-ss", f"{s0:.3f}", "-t", f"{d:.3f}", "-i", video], t0, d)
        await _encode(args, flt, seg, vb, has_audio)
        segs.append(seg)
        starts.append((t0, d))
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
    mids = [a + d / 2 for a, d in starts]
    return {"total": dur, "ordered": True, "frames": await _frames(out, mids, order, cw, ph, tmp)}
