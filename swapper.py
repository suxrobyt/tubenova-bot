"""Video joyini almashtirish: 4 panelli Tiles Hop videosida personajlar joyini o'zgartiradi."""
import asyncio
import os
import re

import numpy as np

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


def _filter(order, cw, h):
    parts = ["[0:v]split=4" + "".join(f"[a{i}]" for i in range(4))]
    for c, old in enumerate(order):
        parts.append(f"[a{c}]crop={cw}:{h}:{old * cw}:0[c{c}]")
    parts.append("".join(f"[c{c}]" for c in range(4)) + "hstack=inputs=4[v]")
    return ";".join(parts)


async def build(video: str, order: list, out: str, tmp: str, progress=None):
    """order[j] = yangi j-o'rindagi panelning eski indeksi (0..3). (davomiylik, ketma-ketmi) qaytaradi."""
    w, h, dur, has_audio = await _probe(video)
    if dur > MAX_SEC:
        raise ValueError(f"Video juda uzun (eng ko'pi {MAX_SEC // 60} daqiqa).")
    cw = (w // 4) // 2 * 2
    ph = h // 2 * 2
    flt = _filter(order, cw, ph)
    vb = int(min(5000, max(300, render.TARGET_MB * 8192 / dur - 128)))
    if progress:
        await progress("🔎 Panellar va o'ynash tartibi aniqlanmoqda...")
    sequential = True
    try:
        bounds = find_segments(await _motion(video))
    except NotSequential:
        sequential = False
    if not sequential:
        if progress:
            await progress("🎬 Panellar joyi almashtirilmoqda...")
        await _encode(["-i", video], flt, out, vb, has_audio)
        return dur, False
    times = [b / FPS for b in bounds]
    times[-1] = dur
    segs = []
    for j, old in enumerate(order):
        if progress:
            await progress(f"🎬 {j + 1}-qism tayyorlanmoqda ({j + 1}/4)...")
        s, e = times[old], times[old + 1]
        seg = os.path.join(tmp, f"sw{j}.mp4")
        await _encode(["-ss", f"{s:.3f}", "-t", f"{e - s:.3f}", "-i", video], flt, seg, vb, has_audio)
        segs.append(seg)
    if progress:
        await progress("🔗 Qismlar birlashtirilmoqda...")
    lst = os.path.join(tmp, "swlist.txt")
    with open(lst, "w") as f:
        f.write("".join(f"file '{s}'\n" for s in segs))
    rc, err = await _run(["-y", "-f", "concat", "-safe", "0", "-i", lst, "-c", "copy",
                          "-movflags", "+faststart", out])
    if rc != 0:
        raise RuntimeError("ffmpeg birlashtirish xatosi: " + err[-500:])
    if os.path.getsize(out) > 49 * 1024 * 1024:
        raise RuntimeError("Video hajmi 50 MB dan oshib ketdi")
    return dur, True
