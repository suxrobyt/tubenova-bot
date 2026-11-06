"""Video joyini almashtirish: 4 panelli Tiles Hop videosida personajlar joyini o'zgartiradi."""
import asyncio
import os
import re

import numpy as np
from PIL import Image, ImageFilter

import heroes as heroes_mod
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


GW = 480                         # overlay tahlili uchun kadr eni (har panel 120 px)
MARGIN = 0.4                     # segment chegarasidan shuncha soniya tashlab ketiladi
DIFF_THR = 36                    # "muzlagan" panelga nisbatan farq chegarasi
EV_GAP = 5                       # shuncha kadr (10 fps) ichida bo'lsa — bitta hodisa


async def _frames(path: str, fps: int, gw: int, gh: int):
    """(vaqt, RGB kadr) ni oqim bilan beradi (kichraytirilgan)."""
    proc = await asyncio.create_subprocess_exec(
        FFMPEG, "-v", "error", "-i", path, "-an", "-vf", f"fps={fps},scale={gw}:{gh}:flags=area",
        "-f", "rawvideo", "-pix_fmt", "rgb24", "-", stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL)
    fs = gw * gh * 3
    i = 0
    try:
        while True:
            try:
                data = await proc.stdout.readexactly(fs)
            except asyncio.IncompleteReadError:
                break
            yield i / fps, np.frombuffer(data, np.uint8).reshape(gh, gw, 3)
            i += 1
    finally:
        if proc.returncode is None:
            proc.kill()
        await proc.wait()


def _erode(d):
    e = np.zeros_like(d)
    e[1:-1, 1:-1] = d[1:-1, 1:-1] & d[:-2, 1:-1] & d[2:, 1:-1] & d[1:-1, :-2] & d[1:-1, 2:]
    return e


def _seg_of(t, tb):
    for c in range(4):
        if tb[c] <= t < tb[c + 1]:
            return c
    return 3


def _in_margin(t, c, tb):
    return t - tb[c] < MARGIN or tb[c + 1] - t < MARGIN


async def _static_refs(path, gw, gh, tb):
    """Har segment uchun muzlagan (o'ynamayotgan) panellarning toza ko'rinishi: kadrlar medianasi.
    Overlay faqat qisqa vaqt chiqgani uchun median uni yo'qotadi."""
    pw = gw // 4
    refs, cur, buf = {}, None, []

    def flush():
        if cur is not None and len(buf) >= 3:
            refs[cur] = {p: np.median(np.stack([f[:, p * pw:(p + 1) * pw] for f in buf]), axis=0).astype(np.int16)
                         for p in range(4) if p != cur}

    async for t, fr in _frames(path, 2, gw, gh):
        c = _seg_of(t, tb)
        if c != cur:
            flush()
            cur, buf = c, []
        if not _in_margin(t, c, tb):
            buf.append(fr.copy())
    flush()
    return refs


async def _write_mask_video(path, masks, n, gw, gh):
    """masks: {kadr indeksi: bool (gh,gw)}. 10 fps, qora (0) = overlay yo'q."""
    proc = await asyncio.create_subprocess_exec(
        FFMPEG, "-v", "error", "-y", "-f", "rawvideo", "-pix_fmt", "gray", "-s", f"{gw}x{gh}", "-framerate", "10",
        "-i", "-", "-c:v", "ffv1", "-pix_fmt", "gray", path,
        stdin=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
    zero = bytes(gw * gh)
    for i in range(n):
        m = masks.get(i)
        if m is None:
            proc.stdin.write(zero)
        else:
            im = Image.fromarray((m * 255).astype(np.uint8)).filter(ImageFilter.MaxFilter(5))   # biroz kengaytiramiz
            proc.stdin.write(im.tobytes())
        if i % 50 == 0:
            await proc.stdin.drain()
    proc.stdin.close()
    await proc.wait()
    return proc.returncode == 0


async def _overlay(path: str, w: int, h: int, bounds, dur: float, tmp: str):
    """Videoga qo'yilgan overlay'lar (Subscribe, belgilar, yozuvlar va h.k.) — panellarning bir qismi emas.
    Qaytaradi: (niqob videosi yo'li, [(boshlanish, tugash), ...]) yoki None.
    Usul: ketma-ket o'ynashda har segmentda qolgan 3 panel "muzlagan" bo'ladi; ularning toza ko'rinishidan
    (median) har qanday og'ish — overlay. Qo'shimcha: katta oq panel (Subscribe) alohida aniqlanadi."""
    gw = GW
    gh = max(8, round(gw * h / w) // 2 * 2)
    pw = gw // 4
    tb = [b / FPS for b in bounds] if bounds else None
    if tb:
        tb[-1] = dur
    refs = await _static_refs(path, gw, gh, tb) if tb else {}
    min_px = 0.002 * gw * gh
    flagged = {}                                   # kadr -> (maska, o'ynayotgan panel yoki None)
    nframes = 0
    async for t, fr in _frames(path, 10, gw, gh):
        nframes += 1
        i = nframes - 1
        m = np.zeros((gh, gw), bool)
        c = None
        if tb:
            c = _seg_of(t, tb)
            if c in refs and not _in_margin(t, c, tb):
                for p in range(4):
                    if p == c:
                        continue
                    d = (np.abs(fr[:, p * pw:(p + 1) * pw].astype(np.int16) - refs[c][p]).max(axis=-1) > DIFF_THR)
                    d = _erode(d)
                    if d.mean() < 0.4:             # panel muzlamagan bo'lsa, ishonib bo'lmaydi
                        m[:, p * pw:(p + 1) * pw] |= d
        bb = _bar_bbox(fr)
        if bb:
            x0, y0, x1, y1 = bb
            m[max(y0 - 1, 0):y1 + 1, max(x0 - 1, 0):x1 + 1] = True
        if m.sum() >= min_px and len(flagged) < 6000:
            flagged[i] = (m, c)
    if not flagged:
        return None
    # --- hodisalar: ketma-ket belgilangan kadrlar
    idx = sorted(flagged)
    events, cur = [], [idx[0]]
    for i in idx[1:]:
        if i - cur[-1] <= EV_GAP:
            cur.append(i)
        else:
            events.append(cur)
            cur = [i]
    events.append(cur)
    events = [e for e in events if len(e) >= 3]
    if not events:
        return None

    def bbox(m):
        ys, xs = np.where(m)
        return xs.min(), ys.min(), xs.max(), ys.max()

    unions = [np.logical_or.reduce([flagged[i][0] for i in e]) for e in events]
    # --- bir joydagi hodisalarni birlashtiramiz (har xil panellar muzlaganda overlay'ning turli bo'lagi ko'rinadi)
    groups = [[k] for k in range(len(events))]
    boxes = [bbox(u) for u in unions]

    def touch(a, b):
        return not (a[2] + 4 < b[0] or b[2] + 4 < a[0] or a[3] + 4 < b[1] or b[3] + 4 < a[1])

    changed = True
    while changed:
        changed = False
        for x in range(len(groups)):
            for y in range(x + 1, len(groups)):
                bx = [min(boxes[k][0] for k in groups[x]), min(boxes[k][1] for k in groups[x]),
                      max(boxes[k][2] for k in groups[x]), max(boxes[k][3] for k in groups[x])]
                by = [min(boxes[k][0] for k in groups[y]), min(boxes[k][1] for k in groups[y]),
                      max(boxes[k][2] for k in groups[y]), max(boxes[k][3] for k in groups[y])]
                if touch(bx, by):
                    groups[x] += groups.pop(y)
                    changed = True
                    break
            if changed:
                break
    masks, ranges, clusters = {}, [], []
    for g in groups:
        U = np.logical_or.reduce([unions[k] for k in g])
        if U.mean() > 0.3:                          # juda katta — overlay emas, panelning o'zi o'zgargan
            continue
        for k in g:
            e = events[k]
            last = None
            for i in range(max(e[0] - 1, 0), min(e[-1] + 2, nframes)):
                if i in flagged:
                    m, c = flagged[i]
                    last = m.copy()
                    if c is not None:              # o'ynayotgan panel ostidagi qismini boshqa hodisalardan to'ldiramiz
                        last[:, c * pw:(c + 1) * pw] |= U[:, c * pw:(c + 1) * pw]
                if last is not None:
                    masks[i] = last
            ranges.append((max(e[0] / 10 - 0.15, 0.0), e[-1] / 10 + 0.25))
        kx, ky = w / gw, h / gh
        x0, y0, x1, y1 = bbox(U)
        clusters.append(((int(x0 * kx), int(y0 * ky), int((x1 + 1) * kx), int((y1 + 1) * ky)),
                         [(max(events[k][0] / 10 - 0.15, 0.0), events[k][-1] / 10 + 0.25) for k in g]))
    if not masks:
        return None
    out = os.path.join(tmp, "ovmask.mkv")
    if not await _write_mask_video(out, masks, nframes, gw, gh):
        return None
    return out, ranges, clusters


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


async def _encode(args_in, filt, out, vb, audio, dur):
    args = ["-y"] + args_in + ["-filter_complex", filt, "-map", "[v]"]
    if audio:
        args += ["-map", "0:a:0?", "-af", "aresample=44100,aformat=channel_layouts=stereo",
                 "-c:a", "aac", "-b:a", "128k"]
    args += ["-c:v", "libx264", "-preset", "veryfast", "-threads", "2", "-r", str(FPS),
             "-b:v", f"{vb}k", "-maxrate", f"{int(vb * 1.4)}k", "-bufsize", f"{vb * 2}k",
             "-g", "60", "-keyint_min", "60", "-sc_threshold", "0", "-pix_fmt", "yuv420p",
             "-t", f"{dur:.3f}", out]
    rc, err = await _run(args)
    if rc != 0:
        raise RuntimeError("ffmpeg xatosi: " + err[-500:])


def _filter(order, cw, h, old, apply=None, clean=None, dlogo=()):
    """order[k] = k-o'rindagi panelning eski indeksi; old = shu qismda o'ynayotgan eski panel.
    clean = (ref_kirish, niqob_kirish): panellardan overlay olib tashlanadi (muzlaganlar — toza rasm bilan,
        o'ynayotgani — xira parcha bilan); niqob ASL vaqt bo'yicha.
    apply = (video_kirish, niqob_kirish): overlay asl joyida va asl vaqtida asl videodan qayta qo'yiladi."""
    w4 = 4 * cw
    parts = ["[0:v]split=4" + "".join(f"[a{i}]" for i in range(4))]
    frozen = [q for q in range(4) if q != old]
    if clean:
        ir, im = clean
        parts.append(f"[{im}:v]fps={FPS},scale={w4}:{h}:flags=bilinear,format=gray,boxblur=2:1,split=3"
                     + "".join(f"[cm{q}]" for q in frozen))
        parts.append(f"[{ir}:v]format=rgba,split=3" + "".join(f"[rf{q}]" for q in frozen))
    for k, q in enumerate(order):
        if not clean:
            parts.append(f"[a{q}]crop={cw}:{h}:{q * cw}:0[c{k}]")
            continue
        parts.append(f"[a{q}]crop={cw}:{h}:{q * cw}:0[r{k}]")
        if q != old:
            parts.append(f"[cm{q}]crop={cw}:{h}:{q * cw}:0[cmc{q}]")
            parts.append(f"[rf{q}]crop={cw}:{h}:{q * cw}:0[rc{q}]")
            parts.append(f"[rc{q}][cmc{q}]alphamerge[rm{q}]")
            parts.append(f"[r{k}][rm{q}]overlay=0:0:format=auto[c{k}]")
        else:
            # o'ynayotgan panel: toza kadr yo'q, shuning uchun overlay joyi atrofdan to'ldiriladi (delogo)
            chain = ",".join(
                f"delogo=x={x}:y={y}:w={bw}:h={bh}:enable='" + "+".join(f"between(t,{p:.3f},{q2:.3f})" for p, q2 in rg) + "'"
                for x, y, bw, bh, rg in dlogo) or "null"
            parts.append(f"[r{k}]{chain}[c{k}]")
    last = "base" if apply else "v"
    parts.append("".join(f"[c{k}]" for k in range(4)) + f"hstack=inputs=4[{last}]")
    if apply:
        iv, im = apply
        parts.append(f"[{iv}:v]crop={w4}:{h}:0:0,format=rgba[ovc]")
        parts.append(f"[{im}:v]fps={FPS},scale={w4}:{h}:flags=bilinear,format=gray,boxblur=2:1[msk]")
        parts.append("[ovc][msk]alphamerge[ova]")
        parts.append("[base][ova]overlay=0:0:format=auto[v]")
    return ";".join(parts)


def _local_ranges(ranges, t0, d):
    return [(max(a, t0), min(b, t0 + d)) for a, b in ranges if min(b, t0 + d) > max(a, t0)]


async def _clean_refs(video, tb, ranges, cw, ph, tmp, need):
    """need: toza rasm kerak bo'lgan segmentlar (eski panel indekslari). Har biri uchun to'liq o'lchamli
    rasm yasaydi: o'sha segmentda muzlagan panellarning median kadri (overlay yo'q paytdagi kadrlardan)."""
    out = {}
    for c in need:
        lo, hi = tb[c] + MARGIN, tb[c + 1] - MARGIN
        if hi - lo < 0.5:
            continue
        times = [lo + (hi - lo) * (k + 0.5) / 9 for k in range(9)]
        free = [t for t in times if not any(a - 0.2 <= t <= b + 0.2 for a, b in ranges)]
        use = free if len(free) >= 3 else times
        frames = []
        for j, t in enumerate(use):
            fp = os.path.join(tmp, f"ref{c}_{j}.png")
            rc, _ = await _run(["-y", "-ss", f"{t:.3f}", "-i", video, "-frames:v", "1", fp], 120)
            if rc == 0 and os.path.exists(fp):
                frames.append(np.array(Image.open(fp).convert("RGB"))[:ph, :4 * cw])
        if not frames:
            continue
        med = np.median(np.stack(frames), axis=0).astype(np.uint8) if len(frames) > 1 else frames[0]
        rp = os.path.join(tmp, f"clean{c}.png")
        Image.fromarray(med).save(rp)
        out[c] = rp
    return out


async def _heroes(video, w, h, cw, tb, dur, order, tmp):
    """Panellarda to'xtab turgan personaj rasmlarini kesib oladi (prevyu uchun), YANGI tartibda.
    Panel o'ynamayotgan paytdagi kadrlardan olinadi; bir nechta kadr medianasi overlay'larni yo'qotadi.
    Qaytaradi: 4 ta RGBA (PIL) yoki []."""
    if tb:
        times = [(tb[s] + tb[s + 1]) / 2 for s in range(4)]
    else:
        times = [dur * (k + 0.5) / 4 for k in range(4)]
    frames = []
    for j, t in enumerate(times):
        fp = os.path.join(tmp, f"hf{j}.png")
        rc, _ = await _run(["-y", "-ss", f"{t:.3f}", "-i", video, "-frames:v", "1", fp], 120)
        frames.append(np.array(Image.open(fp).convert("RGB")) if rc == 0 and os.path.exists(fp) else None)
    panels = []
    for p in range(4):
        use = [f[:, p * cw:(p + 1) * cw] for s, f in enumerate(frames) if f is not None and (not tb or s != p)]
        if not use:
            return []
        panels.append(np.median(np.stack(use), axis=0).astype(np.uint8) if len(use) > 1 else use[0])
    cuts = await asyncio.to_thread(heroes_mod.cutouts, panels)
    out = []
    for p in order:
        c = cuts[p]
        out.append(c if c is not None else Image.fromarray(panels[p]).convert("RGBA"))
    return out


async def build(video: str, order: list, out: str, tmp: str, progress=None):
    """order[j] = yangi j-o'rindagi panelning eski indeksi (0..3).
    Qaytaradi: {"total": soniya, "ordered": ketma-ketmi, "heroes": [prevyu uchun 4 ta RGBA personaj]}"""
    w, h, dur, has_audio = await _probe(video)
    if dur > MAX_SEC:
        raise ValueError(f"Video juda uzun (eng ko'pi {MAX_SEC // 60} daqiqa).")
    cw = (w // 4) // 2 * 2
    ph = h // 2 * 2
    vb = int(min(5000, max(300, render.TARGET_MB * 8192 / dur - 128)))
    if progress:
        await progress("🔎 Panellar va o'ynash tartibi aniqlanmoqda...")
    mo_res = await _motion(video)
    bounds = None
    try:
        bounds = find_segments(mo_res)
    except NotSequential:
        pass
    if progress:
        await progress("🔎 Subscribe va panellar ustidagi boshqa narsalar aniqlanmoqda...")
    try:
        ov = await _overlay(video, w, h, bounds, dur, tmp)
    except Exception:
        ov = None

    def delogos(s0, d, old):
        """O'ynayotgan panel ustidagi overlay joylari: [(x, y, eni, bo'yi, [(a, b)...])] (panel koordinatalarida)."""
        res = []
        for (x0, y0, x1, y1), rg in ov[2]:
            lr = _local_ranges(rg, s0, d)
            lx0, lx1 = max(x0, old * cw), min(x1, (old + 1) * cw)
            if not lr or lx1 <= lx0:
                continue
            px0, px1 = max(lx0 - old * cw - 4, 1), min(lx1 - old * cw + 4, cw - 2)
            py0, py1 = max(y0 - 4, 1), min(y1 + 4, ph - 2)
            if px1 - px0 > 2 and py1 - py0 > 2:
                res.append((px0, py0, px1 - px0, py1 - py0, [(p - s0, q - s0) for p, q in lr]))
        return res

    def inputs(main_args, t0, d, s0=None, old=None):
        """(kirishlar, apply, clean): s0/old berilsa (ketma-ket rejim), overlay asl panel kadrlaridan ham tozalanadi."""
        args, apply, clean = list(main_args), None, None
        n = 1
        if ov and _local_ranges(ov[1], t0, d):
            args += ["-ss", f"{t0:.3f}", "-t", f"{d:.3f}", "-i", video,
                     "-ss", f"{t0:.3f}", "-t", f"{d:.3f}", "-i", ov[0]]
            apply = (n, n + 1)
            n += 2
        if ov and s0 is not None and old in clean_refs and _local_ranges(ov[1], s0, d):
            args += ["-loop", "1", "-framerate", str(FPS), "-t", f"{d:.3f}", "-i", clean_refs[old],
                     "-ss", f"{s0:.3f}", "-t", f"{d:.3f}", "-i", ov[0]]
            clean = (n, n + 1)
        return args, apply, clean

    tb = None
    if bounds:
        tb = [b / FPS for b in bounds]
        tb[-1] = dur
    if not bounds:
        if progress:
            await progress("🎬 Panellar joyi almashtirilmoqda...")
        args, apply, _ = inputs(["-i", video], 0.0, dur)
        await _encode(args, _filter(order, cw, ph, -1, apply), out, vb, has_audio, dur)
        return {"total": dur, "ordered": False,
                "heroes": await _heroes(video, w, h, cw, None, dur, order, tmp)}
    clean_refs = {}
    if ov:
        if progress:
            await progress("🧹 Panellar ustidagi narsalar tozalanmoqda...")
        need = [c for c in range(4) if _local_ranges(ov[1], tb[c], tb[c + 1] - tb[c])]
        clean_refs = await _clean_refs(video, tb, ov[1], cw, ph, tmp, need)
    segs, t0 = [], 0.0
    for j, old in enumerate(order):
        if progress:
            await progress(f"🎬 {j + 1}-qism tayyorlanmoqda ({j + 1}/4)...")
        s0, e0 = tb[old], tb[old + 1]
        d = e0 - s0
        seg = os.path.join(tmp, f"sw{j}.mp4")
        args, apply, clean = inputs(["-ss", f"{s0:.3f}", "-t", f"{d:.3f}", "-i", video], t0, d, s0, old)
        dl = delogos(s0, d, old) if clean else ()
        await _encode(args, _filter(order, cw, ph, old, apply, clean, dl), seg, vb, has_audio, d)
        segs.append(seg)
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
    return {"total": dur, "ordered": True,
            "heroes": await _heroes(video, w, h, cw, tb, dur, order, tmp)}
