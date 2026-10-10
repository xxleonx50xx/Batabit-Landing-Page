#!/usr/bin/env python3
"""
track.py — Rastreo de puntos fijos del terreno para anclar el overlay a un video aéreo en movimiento.

Toma los puntos "anchors" de route.json (coordenadas del fotograma 0, en píxeles nativos del video),
los sigue fotograma a fotograma por correlación cruzada normalizada (sin OpenCV: solo numpy + ffmpeg)
y ajusta una homografía por fotograma (fotograma 0 -> fotograma t). El resultado se guarda en
tracking.json, que overlay.js y render.py usan para mover la ruta y el pin junto con la cámara.

Uso:
    python3 track.py --video video_mapa.mov --config route.json --out tracking.json

Si la cámara del video está totalmente fija, no hace falta ejecutar esto: pon "tracking": null en
route.json (o borra tracking.json) y el overlay usará la identidad.
"""
import argparse
import json
import subprocess
import sys

import numpy as np
from numpy.lib.stride_tricks import sliding_window_view


def probe(video):
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "v:0",
         "-show_entries", "stream=width,height,r_frame_rate,nb_frames",
         "-of", "json", video],
        capture_output=True, text=True, check=True).stdout
    s = json.loads(out)["streams"][0]
    num, den = s["r_frame_rate"].split("/")
    return int(s["width"]), int(s["height"]), float(num) / float(den)


def read_gray_frames(video, w, h):
    """Decodifica todo el video en escala de grises a resolución de trabajo (w x h)."""
    cmd = ["ffmpeg", "-v", "error", "-i", video, "-vf", f"scale={w}:{h}",
           "-f", "rawvideo", "-pix_fmt", "gray", "-"]
    data = subprocess.run(cmd, capture_output=True, check=True).stdout
    n = len(data) // (w * h)
    return np.frombuffer(data, np.uint8)[: n * w * h].reshape(n, h, w)


def ncc_match(frame, template, cx, cy, search):
    """Busca `template` alrededor de (cx, cy) en `frame`. Devuelve (x, y, score) con precisión subpíxel."""
    th, tw = template.shape
    H, W = frame.shape
    x0 = int(round(cx)) - tw // 2 - search
    y0 = int(round(cy)) - th // 2 - search
    x0 = max(0, min(W - tw - 2 * search, x0))
    y0 = max(0, min(H - th - 2 * search, y0))
    region = frame[y0: y0 + th + 2 * search, x0: x0 + tw + 2 * search].astype(np.float32)
    win = sliding_window_view(region, (th, tw))                    # (2s+1, 2s+1, th, tw)
    t = template.astype(np.float32)
    t = t - t.mean()
    tn = np.sqrt((t * t).sum()) + 1e-6
    wm = win - win.mean(axis=(2, 3), keepdims=True)
    num = np.einsum("ijkl,kl->ij", wm, t)
    den = np.sqrt((wm * wm).sum(axis=(2, 3))) * tn + 1e-6
    score = num / den
    iy, ix = np.unravel_index(int(score.argmax()), score.shape)

    def subpix(a, b, c):
        d = a - 2 * b + c
        return 0.0 if abs(d) < 1e-9 else float(np.clip(0.5 * (a - c) / d, -0.5, 0.5))

    dx = subpix(score[iy, ix - 1], score[iy, ix], score[iy, ix + 1]) if 0 < ix < score.shape[1] - 1 else 0.0
    dy = subpix(score[iy - 1, ix], score[iy, ix], score[iy + 1, ix]) if 0 < iy < score.shape[0] - 1 else 0.0
    return x0 + ix + dx + tw / 2.0, y0 + iy + dy + th / 2.0, float(score[iy, ix])


def track_point(frames, x, y, tpl=48, search=14, retemplate_every=20, min_score=0.45):
    """Sigue un punto a lo largo de todos los fotogramas. Devuelve array (n, 2) y lista de scores."""
    n = len(frames)
    pos = np.zeros((n, 2), np.float64)
    scores = np.zeros(n)
    pos[0] = (x, y)
    scores[0] = 1.0

    def cut(frame, px, py):
        x0 = int(round(px)) - tpl // 2
        y0 = int(round(py)) - tpl // 2
        return frame[y0: y0 + tpl, x0: x0 + tpl].copy(), x0 + tpl / 2.0 - px, y0 + tpl / 2.0 - py

    template, offx, offy = cut(frames[0], x, y)
    for i in range(1, n):
        px, py = pos[i - 1]
        mx, my, sc = ncc_match(frames[i], template, px + offx, py + offy, search)
        pos[i] = (mx - offx, my - offy)
        scores[i] = sc
        if i % retemplate_every == 0 and sc >= min_score:
            template, offx, offy = cut(frames[i], pos[i, 0], pos[i, 1])
    return pos, scores


def gaussian_smooth(arr, sigma):
    """Suavizado temporal 1D (eje 0) con bordes replicados."""
    if sigma <= 0:
        return arr
    r = int(3 * sigma)
    k = np.exp(-0.5 * (np.arange(-r, r + 1) / sigma) ** 2)
    k /= k.sum()
    padded = np.concatenate([np.repeat(arr[:1], r, axis=0), arr, np.repeat(arr[-1:], r, axis=0)])
    out = np.empty_like(arr)
    for c in range(arr.shape[1]):
        out[:, c] = np.convolve(padded[:, c], k, mode="valid")
    return out


def fit_homography(src, dst, weights=None):
    """DLT con normalización y pesos opcionales. src, dst: (n, 2) con n >= 4."""
    if weights is None:
        weights = np.ones(len(src))
    def normalize(p):
        m = p.mean(axis=0)
        s = np.sqrt(2) / (np.sqrt(((p - m) ** 2).sum(axis=1)).mean() + 1e-12)
        T = np.array([[s, 0, -s * m[0]], [0, s, -s * m[1]], [0, 0, 1]])
        ph = np.c_[p, np.ones(len(p))] @ T.T
        return ph, T
    s, Ts = normalize(src)
    d, Td = normalize(dst)
    A = []
    for (x, y, _), (u, v, _), wgt in zip(s, d, np.sqrt(weights)):
        A.append([wgt * c for c in (-x, -y, -1, 0, 0, 0, u * x, u * y, u)])
        A.append([wgt * c for c in (0, 0, 0, -x, -y, -1, v * x, v * y, v)])
    _, _, vt = np.linalg.svd(np.array(A))
    Hn = vt[-1].reshape(3, 3)
    H = np.linalg.inv(Td) @ Hn @ Ts
    return H / H[2, 2]


def fit_similarity(src, dst, weights=None):
    """Ajuste de similitud (escala + rotación + traslación) por mínimos cuadrados con pesos. Devuelve 3x3."""
    if weights is None:
        weights = np.ones(len(src))
    A, b = [], []
    for (x, y), (u, v), wgt in zip(src, dst, np.sqrt(weights)):
        A.append([wgt * x, -wgt * y, wgt, 0]); b.append(wgt * u)
        A.append([wgt * y, wgt * x, 0, wgt]); b.append(wgt * v)
    a, bb, tx, ty = np.linalg.lstsq(np.array(A), np.array(b), rcond=None)[0]
    return np.array([[a, -bb, tx], [bb, a, ty], [0, 0, 1.0]])


def apply(H, p):
    ph = np.c_[p, np.ones(len(p))] @ H.T
    return ph[:, :2] / ph[:, 2:3]


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--video", required=True)
    ap.add_argument("--config", default="route.json")
    ap.add_argument("--out", default="tracking.json")
    ap.add_argument("--work-width", type=int, default=1080, help="ancho de trabajo para el rastreo (menor = más rápido)")
    ap.add_argument("--model", choices=["homography", "similarity"], default="homography")
    ap.add_argument("--smooth", type=float, default=1.5, help="sigma (fotogramas) del suavizado temporal")
    args = ap.parse_args()

    cfg = json.load(open(args.config))
    anchors = cfg.get("anchors") or []
    if len(anchors) < 4 and args.model == "homography":
        print(f"Se necesitan >= 4 anchors para homografía (hay {len(anchors)}); usando similitud.")
        args.model = "similarity"
    if len(anchors) < 2:
        sys.exit("route.json necesita al menos 2 'anchors' [x, y] (coordenadas del fotograma 0).")

    W, H, fps = probe(args.video)
    k = args.work_width / W
    ww, wh = args.work_width, int(round(H * k / 2) * 2)
    print(f"Video {W}x{H} @ {fps:.3f} fps — rastreo a {ww}x{wh}")
    frames = read_gray_frames(args.video, ww, wh)
    n = len(frames)
    print(f"{n} fotogramas decodificados")

    tracks, keep, weights = [], [], []
    for a in anchors:
        name = a.get("name", "?")
        x, y = a["xy"]
        pos, sc = track_point(frames, x * k, y * k)
        ok = sc[1:].min() >= 0.35
        print(f"  {name:>18}: score min {sc[1:].min():.2f} medio {sc[1:].mean():.2f} "
              f"desplazamiento final ({(pos[-1][0]-pos[0][0])/k:+.0f}, {(pos[-1][1]-pos[0][1])/k:+.0f}) px"
              + ("" if ok else "  <- DESCARTADO (score bajo)"))
        if ok:
            tracks.append(gaussian_smooth(pos, args.smooth) / k)
            keep.append(name)
            weights.append(float(a.get("weight", 1.0)))
    if len(tracks) < 2:
        sys.exit("Muy pocos anchors rastreados con éxito. Elige puntos con textura (esquinas, letreros).")
    tracks = np.stack(tracks, axis=1)  # (n, m, 2)
    weights = np.array(weights)
    src = tracks[0]

    mats, residuals, per_point = [], [], []
    for i in range(n):
        dst = tracks[i]
        if args.model == "homography" and len(src) >= 4:
            M = fit_homography(src, dst, weights)
        else:
            M = fit_similarity(src, dst, weights)
        err = np.sqrt(((apply(M, src) - dst) ** 2).sum(axis=1))
        residuals.append(float(err.mean()))
        per_point.append(err)
        mats.append([round(float(v), 8) for v in M.flatten()])
    per_point = np.array(per_point)
    for j, name in enumerate(keep):
        print(f"  error {name:>18}: medio {per_point[:, j].mean():.1f} px, máximo {per_point[:, j].max():.1f} px (peso {weights[j]:g})")

    out = {
        "video": {"width": W, "height": H, "fps": fps, "frames": n},
        "model": args.model,
        "anchors": keep,
        "residual_px_mean": round(float(np.mean(residuals)), 3),
        "residual_px_max": round(float(np.max(residuals)), 3),
        "matrices": mats,
        "tracks": {name: [[round(float(x), 2), round(float(y), 2)] for x, y in tracks[:, j]]
                   for j, name in enumerate(keep)},
    }
    json.dump(out, open(args.out, "w"))
    print(f"Modelo {args.model}: error medio {out['residual_px_mean']} px, máximo {out['residual_px_max']} px "
          f"(resolución nativa). Guardado en {args.out}")


if __name__ == "__main__":
    main()
