"""Video yig'ish: 4 ta video ketma-ket o'ynaydi (o'z ovozi bilan), qolgan 3 tasi rasm bilan to'xtab turadi."""
import asyncio
import os
import re

import imageio_ffmpeg

FFMPEG = imageio_ffmpeg.get_ffmpeg_exe()
OUT_H = int(os.getenv("OUT_H", "1080"))        # 1080 yoki 720 (xotira kam bo'lsa 720)
PANEL = OUT_H * 4 // 9                          # har bir panel eni (1080 -> 480)
TARGET_MB = 40                                  # Telegram bot chegarasi 50 MB
MAX_SEG = 300                                   # bitta video eng ko'pi 5 daqiqa
MAX_TOTAL = 900                                 # jami 15 daqiqa


async def _run(args, timeout=1800):
    proc = await asyncio.create_subprocess_exec(
        FFMPEG, *args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    try:
        _, err = await asyncio.wait_for(proc.communicate(), timeout)
    except asyncio.TimeoutError:
        proc.kill()
        raise RuntimeError("ffmpeg vaqti tugadi")
    return proc.returncode, err.decode("utf-8", "ignore")


async def info(path: str):
    """(davomiylik soniyada, ovoz bormi)"""
    _, err = await _run(["-hide_banner", "-i", path], 60)
    m = re.search(r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)", err)
    if not m or "Video:" not in err:
        raise ValueError("Videolardan birini o'qib bo'lmadi. Boshqa video yuboring.")
    h, mi, s = m.groups()
    return int(h) * 3600 + int(mi) * 60 + float(s), "Audio:" in err


async def _segment(k, video, d, has_audio, panels, out, vb):
    n = len(panels)
    args = ["-y", "-i", video]
    idx = {}
    for j in range(n):
        if j != k:
            args += ["-loop", "1", "-framerate", "30", "-t", f"{d:.3f}", "-i", panels[j]]
            idx[j] = len(idx) + 1
    if not has_audio:
        args += ["-f", "lavfi", "-t", f"{d:.3f}", "-i", "anullsrc=r=44100:cl=stereo"]
    parts = []
    for j in range(n):
        if j == k:
            parts.append(f"[0:v]scale={PANEL}:{OUT_H}:force_original_aspect_ratio=increase,"
                         f"crop={PANEL}:{OUT_H},setsar=1,fps=30,format=yuv420p[c{j}]")
        else:
            parts.append(f"[{idx[j]}:v]scale={PANEL}:{OUT_H},setsar=1,fps=30,format=yuv420p[c{j}]")
    parts.append("".join(f"[c{j}]" for j in range(n)) + f"hstack=inputs={n}[v]")
    if has_audio:
        parts.append("[0:a:0]aresample=44100,aformat=channel_layouts=stereo,apad[a]")
        amap = "[a]"
    else:
        amap = f"{len(idx) + 1}:a"
    args += ["-filter_complex", ";".join(parts), "-map", "[v]", "-map", amap,
             "-c:v", "libx264", "-preset", "veryfast", "-threads", "2",
             "-b:v", f"{vb}k", "-maxrate", f"{int(vb * 1.4)}k", "-bufsize", f"{vb * 2}k",
             "-g", "60", "-keyint_min", "60", "-sc_threshold", "0", "-pix_fmt", "yuv420p",
             "-c:a", "aac", "-ar", "44100", "-ac", "2", "-b:a", "128k",
             "-t", f"{d:.3f}", out]
    rc, err = await _run(args)
    if rc != 0:
        raise RuntimeError("ffmpeg xatosi: " + err[-500:])


async def build(videos: list, panels: list, out: str, tmp: str, progress=None) -> float:
    """1-video ovozi bilan o'ynaydi (qolgan panellar rasm), keyin 2-, 3-, 4-video. Jami davomiylikni qaytaradi."""
    infos = [await info(v) for v in videos]
    durs = [d for d, _ in infos]
    if min(durs) < 1:
        raise ValueError("Videolardan biri juda qisqa.")
    if max(durs) > MAX_SEG or sum(durs) > MAX_TOTAL:
        raise ValueError(f"Videolar juda uzun (har biri {MAX_SEG // 60} daqiqagacha, jami {MAX_TOTAL // 60} daqiqagacha).")
    total = sum(durs)
    scale = 1.0
    for _ in range(2):
        vb = int(min(5000, max(250, TARGET_MB * scale * 8192 / total - 128)))
        segs = []
        for k, (v, (d, has_a)) in enumerate(zip(videos, infos)):
            if progress:
                await progress(f"🎬 {k + 1}-qism tayyorlanmoqda ({k + 1}/{len(videos)})...")
            seg = os.path.join(tmp, f"seg{k}.mp4")
            await _segment(k, v, d, has_a, panels, seg, vb)
            segs.append(seg)
        if progress:
            await progress("🔗 Qismlar birlashtirilmoqda...")
        lst = os.path.join(tmp, "list.txt")
        with open(lst, "w") as f:
            f.write("".join(f"file '{s}'\n" for s in segs))
        rc, err = await _run(["-y", "-f", "concat", "-safe", "0", "-i", lst, "-c", "copy",
                              "-movflags", "+faststart", out])
        if rc != 0:
            raise RuntimeError("ffmpeg birlashtirish xatosi: " + err[-500:])
        if os.path.getsize(out) <= 49 * 1024 * 1024:
            return total
        scale *= 0.7
    raise RuntimeError("Video hajmi 50 MB dan oshib ketdi")
