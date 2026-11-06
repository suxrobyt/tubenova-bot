"""Panellardagi to'xtab turgan personajlarni (hero) kadrdan kesib olish: prevyu uchun.

Usul: 4 ta panelning umumiy fonini (o'yin interfeysi) panellar orasidagi "ko'pchilik rang" bilan topamiz,
har panelni shu fon bilan solishtiramiz (fon ±R px siljigan bo'lishi mumkin), qolgan joy — personaj.
Keyin GrabCut (cv2 bo'lsa) bilan chetlarini aniqlashtiramiz.
"""
import numpy as np
from PIL import Image, ImageFilter

try:
    import cv2
except Exception:  # cv2 yo'q bo'lsa, oddiy siluet ishlatiladi
    cv2 = None


def mode_bg(panels, tol=30):
    """Har piksel uchun panellar orasida eng ko'p uchraydigan (o'xshash) rang = umumiy fon."""
    a = np.stack([p.astype(np.int16) for p in panels])
    n = len(panels)
    cnt = np.zeros(a.shape[:3], dtype=np.int8)
    for i in range(n):
        for j in range(n):
            if i != j:
                cnt[i] += (np.abs(a[i] - a[j]).max(axis=-1) <= tol)
    best = cnt.argmax(axis=0)
    bg = np.take_along_axis(a, best[None, ..., None], axis=0)[0]
    amb = cnt.max(axis=0) == 0
    if amb.any():
        med = np.median(a, axis=0).astype(np.int16)
        bg[amb] = med[amb]
    return bg.astype(np.uint8)


def _silhouette(p_blur, bg):
    d = np.abs(p_blur.astype(np.int16) - bg.astype(np.int16)).max(axis=-1) > 45
    m = Image.fromarray((d * 255).astype(np.uint8))
    m = m.filter(ImageFilter.MinFilter(9)).filter(ImageFilter.MaxFilter(9))      # ingichka chiziqlarni olib tashlash
    m = m.filter(ImageFilter.MaxFilter(15)).filter(ImageFilter.MinFilter(15))    # teshiklarni yopish
    return np.array(m) > 0


def _keep_main(mask):
    """Eng katta bo'lakni va unga yaqin katta bo'laklarni qoldiradi (maydalarini tashlaydi)."""
    if cv2 is None:
        return mask
    n, lab, st, _ = cv2.connectedComponentsWithStats(mask.astype(np.uint8), connectivity=8)
    if n <= 1:
        return mask
    areas = st[1:, cv2.CC_STAT_AREA]
    big = areas.max()
    keep = np.zeros(n, bool)
    keep[1:] = areas >= 0.12 * big
    return keep[lab]


def _refine(panel, sil):
    """GrabCut bilan siluet chetlarini aniqlashtiradi."""
    if cv2 is None:
        return sil
    h, w = sil.shape
    sc = 540 / h if h > 540 else 1.0
    p = cv2.resize(panel, (max(1, int(w * sc)), max(1, int(h * sc))), interpolation=cv2.INTER_AREA) if sc < 1 else panel
    s = cv2.resize(sil.astype(np.uint8), (p.shape[1], p.shape[0]), interpolation=cv2.INTER_NEAREST)
    k = np.ones((3, 3), np.uint8)
    sure_fg = cv2.erode(s, k, iterations=6) > 0
    if sure_fg.sum() < 50:
        return sil
    maybe = cv2.dilate(s, k, iterations=8) > 0
    gm = np.full(s.shape, cv2.GC_BGD, np.uint8)
    gm[maybe] = cv2.GC_PR_BGD
    gm[s > 0] = cv2.GC_PR_FGD
    gm[sure_fg] = cv2.GC_FGD
    try:
        cv2.grabCut(cv2.cvtColor(p, cv2.COLOR_RGB2BGR), gm, None, np.zeros((1, 65)), np.zeros((1, 65)),
                    4, cv2.GC_INIT_WITH_MASK)
    except Exception:
        return sil
    out = ((gm == cv2.GC_FGD) | (gm == cv2.GC_PR_FGD)).astype(np.uint8)
    out = cv2.resize(out, (w, h), interpolation=cv2.INTER_LINEAR) > 0
    return _keep_main(out)


def cutouts(panels):
    """panels: bir xil o'lchamli RGB (numpy) rasmlar ro'yxati. Har biri uchun shaffof fonli RGBA personaj
    (PIL) qaytaradi; ajratib bo'lmasa o'sha indeksga None."""
    if len(panels) < 3:
        return [None] * len(panels)
    bl = [np.array(Image.fromarray(p).filter(ImageFilter.GaussianBlur(4))) for p in panels]
    bg = mode_bg(bl)
    res = []
    for p, b in zip(panels, bl):
        try:
            sil = _keep_main(_silhouette(b, bg))
            m = _refine(p, sil)
            frac = m.mean()
            if not 0.03 <= frac <= 0.8:
                res.append(None)
                continue
            a = Image.fromarray((m * 255).astype(np.uint8)).filter(ImageFilter.GaussianBlur(1.2))
            img = Image.fromarray(p).convert("RGBA")
            img.putalpha(a)
            res.append(img)
        except Exception:
            res.append(None)
    return res
