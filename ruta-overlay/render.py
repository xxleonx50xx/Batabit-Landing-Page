#!/usr/bin/env python3
"""
render.py — Hornea la animación (ruta verde + pin) sobre el video aéreo y exporta un MP4 (o un PNG/ProRes con alfa).

Requisitos: Python 3.9+, Pillow, numpy y ffmpeg en el PATH.

Ejemplos:
    # Video final a 1080x1920 (rápido, ideal para Reels/TikTok)
    python3 render.py --video video_mapa.mov --out salida_1080.mp4 --scale 0.5

    # Video final a resolución nativa (2160x3840)
    python3 render.py --video video_mapa.mov --out salida_4k.mp4 --scale 1

    # Solo el overlay con canal alfa (ProRes 4444) para montarlo en Premiere / After Effects / DaVinci
    python3 render.py --video video_mapa.mov --alpha overlay_alpha.mov --scale 1

    # Un solo fotograma para revisar (t en segundos)
    python3 render.py --video video_mapa.mov --frame 2.7 --out frame_2_7.png

Toda la geometría, colores y tiempos viven en route.json; el movimiento de cámara en tracking.json (ver track.py).
"""
import argparse
import json
import math
import os
import subprocess
import sys

import numpy as np
from PIL import Image, ImageDraw, ImageFilter

# ----------------------------------------------------------------------------- geometría compartida con overlay.js


def catmull_rom(points, samples, alpha=0.5):
    """Spline Catmull-Rom centrípeta que pasa por todos los puntos de control."""
    P = [np.array(p, dtype=float) for p in points]
    if len(P) < 2:
        return np.array(P)
    if len(P) == 2:
        return np.linspace(P[0], P[1], samples)
    P = [P[0] - (P[1] - P[0])] + P + [P[-1] + (P[-1] - P[-2])]
    nseg = len(P) - 3
    per = max(4, samples // nseg)
    out = []
    for i in range(nseg):
        p0, p1, p2, p3 = P[i], P[i + 1], P[i + 2], P[i + 3]
        t0 = 0.0
        t1 = t0 + max(np.linalg.norm(p1 - p0), 1e-6) ** alpha
        t2 = t1 + max(np.linalg.norm(p2 - p1), 1e-6) ** alpha
        t3 = t2 + max(np.linalg.norm(p3 - p2), 1e-6) ** alpha
        last = i == nseg - 1
        for t in np.linspace(t1, t2, per, endpoint=last):
            a1 = (t1 - t) / (t1 - t0) * p0 + (t - t0) / (t1 - t0) * p1
            a2 = (t2 - t) / (t2 - t1) * p1 + (t - t1) / (t2 - t1) * p2
            a3 = (t3 - t) / (t3 - t2) * p2 + (t - t2) / (t3 - t2) * p3
            b1 = (t2 - t) / (t2 - t0) * a1 + (t - t0) / (t2 - t0) * a2
            b2 = (t3 - t) / (t3 - t1) * a2 + (t - t1) / (t3 - t1) * a3
            out.append((t2 - t) / (t2 - t1) * b1 + (t - t1) / (t2 - t1) * b2)
    return np.array(out)


def resample_by_length(poly, n):
    """Re-muestrea la polilínea a n puntos equiespaciados en longitud de arco (velocidad de trazo constante)."""
    seg = np.linalg.norm(np.diff(poly, axis=0), axis=1)
    cum = np.concatenate([[0.0], np.cumsum(seg)])
    targets = np.linspace(0.0, cum[-1], n)
    return np.c_[np.interp(targets, cum, poly[:, 0]), np.interp(targets, cum, poly[:, 1])]


def partial(poly, progress):
    """Puntos visibles de la polilínea para un avance 0..1 (interpola el último punto: trazo continuo)."""
    if progress <= 0:
        return poly[:1]
    if progress >= 1:
        return poly
    f = progress * (len(poly) - 1)
    i = int(math.floor(f))
    u = f - i
    head = poly[i] + (poly[i + 1] - poly[i]) * u
    return np.vstack([poly[: i + 1], head])


def apply_h(H, pts):
    ph = np.c_[pts, np.ones(len(pts))] @ H.T
    return ph[:, :2] / ph[:, 2:3]


# ----------------------------------------------------------------------------- easings


def ease_in_out_cubic(u):
    return 4 * u ** 3 if u < 0.5 else 1 - (-2 * u + 2) ** 3 / 2


def ease_out_cubic(u):
    return 1 - (1 - u) ** 3


def ease_linear(u):
    return u


def ease_out_back(u, s=1.70158):
    c3 = s + 1
    return 1 + c3 * (u - 1) ** 3 + s * (u - 1) ** 2


def ease_out_bounce(u):
    n1, d1 = 7.5625, 2.75
    if u < 1 / d1:
        return n1 * u * u
    if u < 2 / d1:
        u -= 1.5 / d1
        return n1 * u * u + 0.75
    if u < 2.5 / d1:
        u -= 2.25 / d1
        return n1 * u * u + 0.9375
    u -= 2.625 / d1
    return n1 * u * u + 0.984375


EASINGS = {"easeInOutCubic": ease_in_out_cubic, "easeOutCubic": ease_out_cubic, "linear": ease_linear}


def clamp01(x):
    return 0.0 if x < 0 else 1.0 if x > 1 else x


# ----------------------------------------------------------------------------- estado de la animación en el tiempo t


def anim_state(cfg, t):
    """Devuelve (avance_de_la_línea 0..1, escala_del_pin, desplazamiento_vertical_del_pin, visible_pin, u_pin)."""
    ln, pin = cfg["line"], cfg["pin"]
    ease = EASINGS.get(ln.get("easing", "easeInOutCubic"), ease_in_out_cubic)
    progress = ease(clamp01((t - ln["start"]) / max(ln["end"] - ln["start"], 1e-6)))
    pin_t0 = ln["end"] + pin.get("delay", 0.0)
    if t < pin_t0 - 1e-9:
        return progress, 0.0, 0.0, False, 0.0
    u = clamp01((t - pin_t0) / max(pin.get("duration", 0.6), 1e-6))
    if pin.get("animation", "pop") == "drop":
        scale = 1.0
        dy = -pin.get("drop_height", 400) * (1 - ease_out_bounce(u))
    else:
        s0 = pin.get("start_scale", 0.15)
        scale = s0 + (1 - s0) * ease_out_back(u, pin.get("overshoot", 1.70158))
        dy = 0.0
    return progress, scale, dy, True, u


# ----------------------------------------------------------------------------- dibujo (Pillow, con supermuestreo)


def hex_rgb(h):
    h = h.lstrip("#")
    return tuple(int(h[i: i + 2], 16) for i in (0, 2, 4))


def draw_polyline(d, pts, width, color):
    """Polilínea gruesa con uniones y extremos redondeados (un círculo por vértice; evita los picos de joint='curve')."""
    r = width / 2
    if len(pts) > 1:
        d.line([tuple(p) for p in pts], fill=color, width=max(1, int(round(width))))
    for x, y in pts:
        d.ellipse([x - r, y - r, x + r, y + r], fill=color)


def pin_shapes(anchor, scale, dy, cfg_pin, k):
    """Geometría del pin (chincheta) con la punta en `anchor`, escalada alrededor de la punta."""
    ax, ay = anchor[0], anchor[1] + dy * k
    R = cfg_pin["head_radius"] * k * scale
    hgt = cfg_pin["height"] * k * scale
    cw = cfg_pin.get("casing_width", 0) * k * scale
    hole = cfg_pin.get("hole_radius", 0) * k * scale
    d = hgt - R                      # distancia punta -> centro de la cabeza
    cx, cy = ax, ay - d

    def teardrop(radius):
        if d <= radius:
            return None, (cx, cy, radius)
        alpha = math.acos(radius / d)
        tx, ty = radius * math.sin(alpha), radius * math.cos(alpha)
        tip_y = cy + d * radius / R if radius != R else ay   # desplaza la punta para el casing (curva offset)
        tri = [(cx, tip_y), (cx + tx, cy + ty), (cx - tx, cy + ty)]
        return tri, (cx, cy, radius)

    return {
        "casing": teardrop(R + cw) if cw > 0 else None,
        "body": teardrop(R),
        "hole": (cx, cy, hole),
        "tip": (ax, ay),
    }


def fill_teardrop(d, shape, color):
    tri, (cx, cy, r) = shape
    d.ellipse([cx - r, cy - r, cx + r, cy + r], fill=color)
    if tri:
        d.polygon(tri, fill=color)


def blur_region(layer, bbox, radius):
    """Desenfoque gaussiano restringido a una región (más rápido que desenfocar todo el fotograma)."""
    if radius <= 0:
        return layer
    pad = int(radius * 3) + 2
    x0, y0, x1, y1 = bbox
    x0, y0 = max(0, int(x0) - pad), max(0, int(y0) - pad)
    x1, y1 = min(layer.width, int(x1) + pad), min(layer.height, int(y1) + pad)
    if x1 <= x0 or y1 <= y0:
        return layer
    crop = layer.crop((x0, y0, x1, y1)).filter(ImageFilter.GaussianBlur(radius))
    layer.paste(crop, (x0, y0))
    return layer


class OverlayRenderer:
    def __init__(self, cfg, tracking, out_w, out_h, supersample=2):
        self.cfg = cfg
        self.W, self.H = out_w, out_h
        self.ss = supersample
        self.k = out_w / cfg["video"]["width"]           # nativo -> salida
        self.fps = cfg["video"]["fps"]
        self.mats = None
        if tracking:
            self.mats = [np.array(m, dtype=float).reshape(3, 3) for m in tracking["matrices"]]
            self.fps = tracking["video"].get("fps", self.fps)
        n = cfg["line"].get("samples", 600)
        self.poly0 = resample_by_length(catmull_rom(cfg["route"]["points"], n * 2), n)   # coords fotograma 0
        self.colors = {key: hex_rgb(v) for key, v in cfg["colors"].items()}

    def matrix_at(self, t):
        if not self.mats:
            return np.eye(3)
        f = t * self.fps
        i = int(math.floor(f))
        i = max(0, min(len(self.mats) - 1, i))
        j = min(len(self.mats) - 1, i + 1)
        u = clamp01(f - i)
        return self.mats[i] * (1 - u) + self.mats[j] * u

    def render_rgba(self, t):
        """Devuelve la capa RGBA del overlay (tamaño de salida) para el instante t (segundos)."""
        cfg, k, ss = self.cfg, self.k, self.ss
        ln, pin, col = cfg["line"], cfg["pin"], self.colors
        progress, scale, dy, pin_visible, _ = anim_state(cfg, t)
        H = self.matrix_at(t)
        poly = apply_h(H, self.poly0) * k                 # polilínea completa en coords de salida
        vis = partial(poly, progress)
        anchor = poly[-1]

        big = (self.W * ss, self.H * ss)
        layer = Image.new("RGBA", big, (0, 0, 0, 0))
        d = ImageDraw.Draw(layer)
        shadow = Image.new("RGBA", big, (0, 0, 0, 0))
        ds = ImageDraw.Draw(shadow)
        sh_rgb = col.get("shadow", (0, 0, 0))

        # --- línea de ruta: sombra, casing (borde oscuro), trazo principal
        if progress > 0:
            vs = vis * ss
            off = np.array(ln.get("shadow_offset", [0, 0])) * k * ss
            draw_polyline(ds, vs + off, ln["width"] * k * ss, sh_rgb + (int(255 * ln.get("shadow_opacity", 0)),))
            draw_polyline(d, vs, ln["casing_width"] * k * ss, col["line_casing"] + (255,))
            draw_polyline(d, vs, ln["width"] * k * ss, col["line"] + (255,))

        # --- pin de ubicación
        if pin_visible and scale > 0.001:
            shp = pin_shapes(anchor * ss, scale, dy, pin, k * ss)
            tx, ty = shp["tip"]
            rx, ry = 70 * k * ss * scale, 26 * k * ss * scale
            ds.ellipse([tx - rx, ty - ry + 6 * k * ss, tx + rx, ty + ry + 6 * k * ss],
                       fill=sh_rgb + (int(255 * pin.get("shadow_opacity", 0.3)),))
            if shp["casing"]:
                fill_teardrop(d, shp["casing"], col["pin_casing"] + (255,))
            fill_teardrop(d, shp["body"], col["pin"] + (255,))
            cx, cy, hr = shp["hole"]
            if hr > 0:
                d.ellipse([cx - hr, cy - hr, cx + hr, cy + hr], fill=col["pin_hole"] + (255,))

        layer = layer.resize((self.W, self.H), Image.LANCZOS) if ss != 1 else layer
        shadow = shadow.resize((self.W, self.H), Image.LANCZOS) if ss != 1 else shadow
        bbox = shadow.getbbox()
        if bbox:
            shadow = blur_region(shadow, bbox, max(ln.get("shadow_blur", 0), pin.get("shadow_blur", 0)) * k)
        return Image.alpha_composite(shadow, layer)

    def composite(self, frame_rgb, t):
        over = self.render_rgba(t)
        base = frame_rgb.convert("RGBA")
        return Image.alpha_composite(base, over).convert("RGB")


# ----------------------------------------------------------------------------- E/S con ffmpeg


def probe(video):
    out = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries",
                          "stream=width,height,r_frame_rate,nb_frames:format=duration", "-of", "json", video],
                         capture_output=True, text=True, check=True).stdout
    j = json.loads(out)
    s = j["streams"][0]
    num, den = s["r_frame_rate"].split("/")
    return int(s["width"]), int(s["height"]), float(num) / float(den), float(j["format"]["duration"])


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--video", required=True, help="video aéreo original")
    ap.add_argument("--config", default="route.json")
    ap.add_argument("--tracking", default=None, help="tracking.json (por defecto el indicado en route.json)")
    ap.add_argument("--out", default=None, help="MP4 de salida (o PNG si se usa --frame)")
    ap.add_argument("--alpha", default=None, help="exporta SOLO el overlay con canal alfa a este .mov (ProRes 4444)")
    ap.add_argument("--png-seq", default=None, help="carpeta: exporta el overlay como secuencia PNG con alfa")
    ap.add_argument("--scale", type=float, default=0.5, help="escala de salida respecto al video (1 = nativa)")
    ap.add_argument("--ss", type=int, default=2, help="supermuestreo para antialiasing (1-3)")
    ap.add_argument("--crf", type=int, default=17, help="calidad H.264 (menor = mejor)")
    ap.add_argument("--frame", type=float, default=None, help="renderiza solo este instante (s) a PNG")
    ap.add_argument("--no-tracking", action="store_true", help="ignora tracking.json (cámara fija)")
    args = ap.parse_args()

    here = os.path.dirname(os.path.abspath(args.config)) or "."
    cfg = json.load(open(args.config))
    tracking = None
    tpath = args.tracking or cfg.get("tracking")
    if tpath and not args.no_tracking:
        tpath = tpath if os.path.isabs(tpath) else os.path.join(here, tpath)
        if os.path.exists(tpath):
            tracking = json.load(open(tpath))
        else:
            print(f"Aviso: no existe {tpath}; se renderiza con cámara fija (ejecuta track.py para anclar el pin).")

    W, H, fps, duration = probe(args.video)
    cfg["video"].update({"width": W, "height": H, "fps": fps})
    out_w, out_h = int(round(W * args.scale / 2) * 2), int(round(H * args.scale / 2) * 2)
    r = OverlayRenderer(cfg, tracking, out_w, out_h, supersample=max(1, args.ss))
    nframes = int(round(duration * fps))

    if args.frame is not None:
        out = args.out or f"frame_{args.frame:.2f}.png"
        raw = subprocess.run(["ffmpeg", "-v", "error", "-ss", str(args.frame), "-i", args.video, "-frames:v", "1",
                              "-vf", f"scale={out_w}:{out_h}", "-f", "rawvideo", "-pix_fmt", "rgb24", "-"],
                             capture_output=True, check=True).stdout
        if len(raw) < out_w * out_h * 3:
            sys.exit(f"No hay fotograma en t={args.frame}s (el video dura {duration:.3f}s; último fotograma ~{(nframes - 1) / fps:.3f}s).")
        frame = Image.frombytes("RGB", (out_w, out_h), raw)
        r.composite(frame, args.frame).save(out)
        print(f"Fotograma t={args.frame}s guardado en {out}")
        return

    if args.png_seq:
        os.makedirs(args.png_seq, exist_ok=True)
        for i in range(nframes):
            r.render_rgba(i / fps).save(os.path.join(args.png_seq, f"overlay_{i:04d}.png"))
            if i % 10 == 0:
                print(f"  overlay {i}/{nframes}", flush=True)
        print(f"Secuencia PNG con alfa en {args.png_seq}/ ({nframes} fotogramas, {out_w}x{out_h} @ {fps:g} fps)")
        return

    if args.alpha:
        enc = subprocess.Popen(["ffmpeg", "-v", "error", "-y", "-f", "rawvideo", "-pix_fmt", "rgba",
                                "-s", f"{out_w}x{out_h}", "-r", f"{fps}", "-i", "-",
                                "-c:v", "prores_ks", "-profile:v", "4444", "-pix_fmt", "yuva444p10le", args.alpha],
                               stdin=subprocess.PIPE)
        for i in range(nframes):
            enc.stdin.write(r.render_rgba(i / fps).tobytes())
            if i % 10 == 0:
                print(f"  overlay {i}/{nframes}", flush=True)
        enc.stdin.close(); enc.wait()
        print(f"Overlay con alfa guardado en {args.alpha}")
        return

    out = args.out or "salida.mp4"
    dec = subprocess.Popen(["ffmpeg", "-v", "error", "-i", args.video, "-vf", f"scale={out_w}:{out_h}",
                            "-f", "rawvideo", "-pix_fmt", "rgb24", "-"], stdout=subprocess.PIPE)
    enc = subprocess.Popen(["ffmpeg", "-v", "error", "-y",
                            "-f", "rawvideo", "-pix_fmt", "rgb24", "-s", f"{out_w}x{out_h}", "-r", f"{fps}", "-i", "-",
                            "-i", args.video, "-map", "0:v:0", "-map", "1:a?", "-c:a", "aac", "-b:a", "192k",
                            "-c:v", "libx264", "-preset", "medium", "-crf", str(args.crf), "-pix_fmt", "yuv420p",
                            "-movflags", "+faststart", "-shortest", out], stdin=subprocess.PIPE)
    size = out_w * out_h * 3
    i = 0
    while True:
        raw = dec.stdout.read(size)
        if len(raw) < size:
            break
        frame = Image.frombytes("RGB", (out_w, out_h), raw)
        enc.stdin.write(r.composite(frame, i / fps).tobytes())
        if i % 10 == 0:
            print(f"  fotograma {i}/{nframes}", flush=True)
        i += 1
    enc.stdin.close(); enc.wait(); dec.wait()
    print(f"Listo: {out} ({i} fotogramas, {out_w}x{out_h} @ {fps:g} fps)")


if __name__ == "__main__":
    main()
