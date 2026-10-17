"""Coffin Dance: musiqa zarbiga mos montaj (zarbni aniqlash + ffmpeg)."""
import asyncio
import os
import re

import numpy as np

import render
from render import FFMPEG, _run

FPS = 30
MAX_OUT = 67.0                       # tayyor video eng ko'pi 1 daqiqa 7 soniya
ZOOM = 0.14                          # har zarbda yaqinlashish miqdori
VW, VH = (720, 1280) if render.OUT_H <= 720 else (1080, 1920)   # vertikal (Shorts) format


async def _duration(path: str):
    _, err = await _run(["-hide_banner", "-i", path], 60)
    m = re.search(r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)", err)
    if not m:
        return None
    h, mi, s = m.groups()
    return int(h) * 3600 + int(mi) * 60 + float(s)


async def _pcm(path: str, sr: int = 11025, max_sec: int = 75):
    proc = await asyncio.create_subprocess_exec(
        FFMPEG, "-v", "error", "-i", path, "-vn", "-ac", "1", "-ar", str(sr),
        "-t", str(max_sec), "-f", "f32le", "-",
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    out, _ = await proc.communicate()
    if proc.returncode != 0 or len(out) < sr * 4:
        raise ValueError("Musiqani o'qib bo'lmadi yoki juda qisqa. Boshqa fayl yuboring.")
    return np.frombuffer(out, dtype=np.float32), sr


def _onset_env(x, sr, hop=128, win=512):
    n = 1 + (len(x) - win) // hop
    idx = np.arange(win)[None, :] + hop * np.arange(n)[:, None]
    mag = np.abs(np.fft.rfft(x[idx] * np.hanning(win)[None, :], axis=1))
    mag = np.log1p(10 * mag)
    flux = np.concatenate([[0], np.maximum(0, np.diff(mag, axis=0)).sum(axis=1)])
    k = int(0.4 * sr / hop)
    flux = np.maximum(0, flux - np.convolve(flux, np.ones(2 * k + 1) / (2 * k + 1), mode="same"))
    return flux / (flux.max() + 1e-9), sr / hop


def find_beats(x, sr):
    """Temp (davr, soniya) va birinchi zarb vaqtini (faza) topadi."""
    env, fps = _onset_env(x, sr)
    ac = np.correlate(env, env, "full")[len(env) - 1:]
    best_lag, best = None, -1.0
    for lag in range(int(fps * 60 / 180), int(fps * 60 / 70) + 1):
        bpm = 60 * fps / lag
        prior = np.exp(-0.5 * (np.log2(bpm / 120) / 0.55) ** 2)
        sc = (ac[lag] + 0.5 * ac[min(2 * lag, len(ac) - 1)]) * prior
        if sc > best:
            best, best_lag = sc, lag
    p0 = best_lag / fps
    t = np.arange(len(env)) / fps
    top, res = -1.0, (p0, 0.0)
    for p in np.arange(p0 * 0.96, p0 * 1.04, p0 * 0.0005):
        for ph in np.arange(0, p, 0.01):
            times = np.arange(ph, t[-1], p)
            sc = np.interp(times, t, env).sum() / (len(times) ** 0.5)
            if sc > top:
                top, res = sc, (p, ph)
    return res


def _groups(period, phase, out_dur):
    """Zarblar bo'yicha kadr uzunliklari, 4 tadan guruhlangan."""
    bounds = [0.0]
    t = phase
    while t < out_dur - 0.05:
        if t > 0.15:
            bounds.append(t)
        t += period
    bounds.append(out_dur)
    fr = [round(b * FPS) for b in bounds]
    lens = [b - a for a, b in zip(fr, fr[1:]) if b - a > 0]
    groups = []
    first = 0
    if phase > 0.15 and len(lens) > 1:
        groups.append(lens[:1])
        first = 1
    rest = lens[first:]
    groups += [rest[i:i + 4] for i in range(0, len(rest), 4)]
    return groups


async def _phrase(src, src_dur, start, lens, out, vb):
    m = len(lens)
    span = (max(lens) + 3) / FPS
    base = (f"[0:v]fps={FPS},scale={VW}:{VH}:force_original_aspect_ratio=increase,"
            f"crop={VW}:{VH},setsar=1,format=yuv420p")
    parts = [base + (f",split={m}" + "".join(f"[s{k}]" for k in range(m)) if m > 1 else "[s0]")]
    for k, L in enumerate(lens):
        if k % 2 == 0:
            z = f"(1+{ZOOM}*n/{L})"
            pre = f"[s{k}]trim=end_frame={L},setpts=PTS-STARTPTS"
        else:
            z = f"(1+{ZOOM}*(1-n/{L}))"
            pre = f"[s{k}]trim=end_frame={L},setpts=PTS-STARTPTS,reverse,setpts=PTS-STARTPTS"
        parts.append(f"{pre},scale=w='trunc({VW}*{z}/2)*2':h='trunc({VH}*{z}/2)*2':eval=frame,"
                     f"crop={VW}:{VH},setsar=1[b{k}]")
    parts.append("".join(f"[b{k}]" for k in range(m)) + f"concat=n={m}:v=1:a=0[v]")
    args = ["-y"]
    if src_dur < span + 0.5:
        args += ["-stream_loop", "-1"]
    args += ["-ss", f"{start:.3f}", "-t", f"{span:.3f}", "-i", src,
             "-filter_complex", ";".join(parts), "-map", "[v]", "-an",
             "-c:v", "libx264", "-preset", "veryfast", "-threads", "2", "-r", str(FPS),
             "-b:v", f"{vb}k", "-maxrate", f"{int(vb * 1.3)}k", "-bufsize", f"{vb * 2}k",
             "-g", "60", "-keyint_min", "60", "-sc_threshold", "0", "-pix_fmt", "yuv420p", out]
    rc, err = await _run(args)
    if rc != 0:
        raise RuntimeError("ffmpeg (montaj) xatosi: " + err[-500:])


async def build(music: str, video: str, out: str, tmp: str, progress=None) -> float:
    """Musiqa zarbiga mos montaj. Tayyor videoning davomiyligini qaytaradi."""
    x, sr = await _pcm(music)
    total = await _duration(music) or len(x) / sr
    out_dur = min(total, MAX_OUT, len(x) / sr + 0.5)
    if out_dur < 3:
        raise ValueError("Musiqa juda qisqa (kamida 3 soniya kerak).")
    if progress:
        await progress("🎧 Musiqa zarblari aniqlanmoqda...")
    period, phase = await asyncio.to_thread(find_beats, x, sr)
    vdur = await _duration(video)
    if not vdur or vdur < 0.5:
        raise ValueError("Montaj uchun yuborilgan videoni o'qib bo'lmadi.")
    groups = _groups(period, phase, out_dur)
    frames_total = sum(sum(g) for g in groups)
    T = frames_total / FPS
    vb = int(min(6000, max(800, render.TARGET_MB * 8192 / T - 170)))
    clips = []
    for p, lens in enumerate(groups):
        if progress and p % 4 == 0:
            await progress(f"🎞 Montaj: {p + 1}/{len(groups)} qism...")
        span = (max(lens) + 3) / FPS
        start = ((p * 0.6180339887) % 1.0) * max(vdur - span - 0.05, 0.0)
        clip = os.path.join(tmp, f"ph{p}.mp4")
        await _phrase(video, vdur, start, lens, clip, vb)
        clips.append(clip)
    if progress:
        await progress("🔗 Musiqa qo'shilmoqda va birlashtirilmoqda...")
    lst = os.path.join(tmp, "list.txt")
    with open(lst, "w") as f:
        f.write("".join(f"file '{c}'\n" for c in clips))
    fade = max(T - 0.6, 0)
    rc, err = await _run(["-y", "-f", "concat", "-safe", "0", "-i", lst, "-i", music,
                          "-map", "0:v", "-map", "1:a:0", "-c:v", "copy",
                          "-af", f"afade=t=out:st={fade:.2f}:d=0.6", "-c:a", "aac", "-b:a", "160k",
                          "-ar", "44100", "-ac", "2", "-t", f"{T:.3f}", "-movflags", "+faststart", out])
    if rc != 0:
        raise RuntimeError("ffmpeg birlashtirish xatosi: " + err[-500:])
    if os.path.getsize(out) > 49 * 1024 * 1024:
        raise RuntimeError("Video hajmi 50 MB dan oshib ketdi")
    return T
