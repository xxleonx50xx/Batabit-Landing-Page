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
    python3 render.py --video video_mapa.mov --frame 2.9 --out frame_2_9.png

Toda la geometría, colores y tiempos viven en route.json; el movimiento de cámara en tracking.json (ver track.py).
Capas que se componen por fotograma, de abajo hacia arriba:
    sombra -> glow -> borde oscuro + trazo con degradado -> núcleo luminoso + flujo de guiones + cometa
    -> ondas de pulso -> pin (borde, cuerpo con degradado y brillo, hueco) -> etiqueta
"""
import argparse
import json
import math
import os
import subprocess
import sys

import numpy as np
from PIL import Image, ImageDraw, ImageFilter, ImageFont

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
    return np.c_[np.interp(targets, cum, poly[:, 0]), np.interp(targets, cum, poly[:, 1])], float(cum[-1])


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


def ease_out_quad(u):
    return 1 - (1 - u) ** 2


def ease_linear(u):
    return u


def ease_out_back(u, s=1.70158):
    c3 = s + 1
    return 1 + c3 * (u - 1) ** 3 + s * (u - 1) ** 2


EASINGS = {"easeInOutCubic": ease_in_out_cubic, "easeOutCubic": ease_out_cubic, "linear": ease_linear}


def clamp01(x):
    return 0.0 if x < 0 else 1.0 if x > 1 else x


# ----------------------------------------------------------------------------- estado de la animación en el tiempo t


def anim_state(cfg, t):
    """
    Estado de todos los elementos para el instante t (segundos). Devuelve un dict con:
      progress   avance 0..1 de la línea (ya con easing)
      head_alpha opacidad del "cometa" en la punta (1 mientras se dibuja; se desvanece al llegar)
      pin        None o dict: scale_x, scale_y, dy (px nativos, negativo = arriba), height_frac (0 = en el suelo)
      impact     instante en que el pin toca el suelo (None si aún no aparece)
    """
    ln, pin = cfg["line"], cfg["pin"]
    ease = EASINGS.get(ln.get("easing", "easeInOutCubic"), ease_in_out_cubic)
    progress = ease(clamp01((t - ln["start"]) / max(ln["end"] - ln["start"], 1e-6)))
    head = ln.get("head", {})
    if t < ln["start"]:
        head_alpha = 0.0
    elif t <= ln["end"]:
        head_alpha = 1.0
    else:
        head_alpha = 1.0 - clamp01((t - ln["end"]) / max(head.get("fade", 0.3), 1e-6))

    st = {"progress": progress, "head_alpha": head_alpha, "pin": None, "impact": None}
    t0 = ln["end"] + pin.get("delay", 0.0)
    if t < t0 - 1e-9:
        return st
    dur = max(pin.get("duration", 0.6), 1e-6)
    u = clamp01((t - t0) / dur)
    if pin.get("animation", "drop") == "drop":
        fall = pin.get("fall_fraction", 0.5)
        drop_h = pin.get("drop_height", 700)
        s0 = pin.get("drop_start_scale", 0.8)
        if u < fall:
            uf = u / fall
            dy = -drop_h * (1 - uf * uf)                      # caída acelerada (gravedad)
            hf = 1 - uf * uf
            sc = s0 + (1 - s0) * ease_out_quad(uf)
            sx, sy = sc, sc
        else:
            v = (u - fall) / max(1 - fall, 1e-6)
            bounce = pin.get("bounce_height", 70) * 4 * v * (1 - v)   # un rebote parabólico
            dy = -bounce
            hf = bounce / drop_h
            pulse = max(0.0, 1 - abs(u - fall) / max(pin.get("squash_width", 0.1), 1e-6))
            sq = pin.get("squash", 0.16) * pulse                       # aplastamiento al impactar
            sx, sy = 1 + sq, 1 - sq
        st["pin"] = {"scale_x": sx, "scale_y": sy, "dy": dy, "height_frac": hf}
        st["impact"] = t0 + fall * dur
    else:  # pop: escala con sobreimpulso desde la punta
        s0 = pin.get("start_scale", 0.15)
        sc = s0 + (1 - s0) * ease_out_back(u, pin.get("overshoot", 1.70158))
        st["pin"] = {"scale_x": sc, "scale_y": sc, "dy": 0.0, "height_frac": 0.0}
        st["impact"] = t0
    return st


# ----------------------------------------------------------------------------- utilidades de dibujo


def hex_rgb(h):
    h = h.lstrip("#")
    return tuple(int(h[i: i + 2], 16) for i in (0, 2, 4))


def lerp_rgb(a, b, w):
    return tuple(int(round(a[i] + (b[i] - a[i]) * w)) for i in range(3))


def draw_polyline(d, pts, width, color=None, colors=None):
    """Polilínea gruesa con uniones y extremos redondeados. `colors`: color por vértice (degradado)."""
    r = width / 2
    wi = max(1, int(round(width)))
    if colors is None:
        if len(pts) > 1:
            d.line([tuple(p) for p in pts], fill=color, width=wi)
        for x, y in pts:
            d.ellipse([x - r, y - r, x + r, y + r], fill=color)
    else:
        for i in range(len(pts) - 1):
            d.line([tuple(pts[i]), tuple(pts[i + 1])], fill=colors[i], width=wi)
        for i, (x, y) in enumerate(pts):
            d.ellipse([x - r, y - r, x + r, y + r], fill=colors[i])


def blur_bbox(layer, radius):
    """Desenfoque gaussiano restringido al contenido de la capa (mucho más rápido que desenfocar todo)."""
    if radius <= 0:
        return layer
    bbox = layer.getbbox()
    if not bbox:
        return layer
    pad = int(radius * 3) + 2
    x0, y0 = max(0, bbox[0] - pad), max(0, bbox[1] - pad)
    x1, y1 = min(layer.width, bbox[2] + pad), min(layer.height, bbox[3] + pad)
    crop = layer.crop((x0, y0, x1, y1)).filter(ImageFilter.GaussianBlur(radius))
    layer.paste(crop, (x0, y0))
    return layer


def alpha_mul(layer, factor):
    if factor >= 1:
        return layer
    r, g, b, a = layer.split()
    a = a.point(lambda v: int(v * factor))
    layer.putalpha(a)
    return layer


def dash_runs(n, spacing, dash, gap, offset):
    """Tramos de vértices 'encendidos' para un patrón de guiones que avanza `offset` px a lo largo del arco."""
    period = dash + gap
    runs, cur = [], None
    for i in range(n):
        on = ((i * spacing - offset) % period) < dash
        if on and cur is None:
            cur = i
        elif not on and cur is not None:
            runs.append((cur, i))
            cur = None
    if cur is not None:
        runs.append((cur, n))
    return runs


def load_font(cfg_dir, hint, size):
    candidates = [hint, os.path.join(cfg_dir, "fonts", "DMSans-Bold.ttf"),
                  "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
                  "C:/Windows/Fonts/arialbd.ttf", "/Library/Fonts/Arial Bold.ttf",
                  "/System/Library/Fonts/Supplemental/Arial Bold.ttf"]
    for c in candidates:
        if not c:
            continue
        p = c if os.path.isabs(c) else os.path.join(cfg_dir, c)
        if os.path.exists(p):
            try:
                return ImageFont.truetype(p, max(1, int(size)))
            except OSError:
                pass
    return ImageFont.load_default()


# ----------------------------------------------------------------------------- renderizador


class OverlayRenderer:
    def __init__(self, cfg, tracking, out_w, out_h, supersample=2, cfg_dir="."):
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
        self.poly0, self.length0 = resample_by_length(catmull_rom(cfg["route"]["points"], n * 2), n)
        self.spacing0 = self.length0 / (n - 1)           # px nativos entre vértices
        self.colors = {key: hex_rgb(v) for key, v in cfg["colors"].items()}
        lab = cfg["pin"].get("label", {})
        self.font = load_font(cfg_dir, lab.get("font"), lab.get("font_size", 58) * self.k * self.ss)

    # --- cámara
    def matrix_at(self, t):
        if not self.mats:
            return np.eye(3)
        f = t * self.fps
        i = max(0, min(len(self.mats) - 1, int(math.floor(f))))
        j = min(len(self.mats) - 1, i + 1)
        u = clamp01(f - i)
        return self.mats[i] * (1 - u) + self.mats[j] * u

    # --- capa completa del overlay para el instante t
    def render_rgba(self, t):
        cfg, k, ss, col = self.cfg, self.k, self.ss, self.colors
        ln, pin = cfg["line"], cfg["pin"]
        st = anim_state(cfg, t)
        poly = apply_h(self.matrix_at(t), self.poly0) * k              # polilínea completa, coords de salida
        vis = partial(poly, st["progress"])
        anchor = poly[-1]
        n_vis = len(vis)

        size1, size_ss = (self.W, self.H), (self.W * ss, self.H * ss)
        shadow = Image.new("RGBA", size1, (0, 0, 0, 0)); d_sh = ImageDraw.Draw(shadow)
        glow = Image.new("RGBA", size1, (0, 0, 0, 0)); d_gl = ImageDraw.Draw(glow)
        line = Image.new("RGBA", size_ss, (0, 0, 0, 0)); d_ln = ImageDraw.Draw(line)
        fx = Image.new("RGBA", size_ss, (0, 0, 0, 0)); d_fx = ImageDraw.Draw(fx)
        headglow = Image.new("RGBA", size1, (0, 0, 0, 0)); d_hg = ImageDraw.Draw(headglow)
        pinl = Image.new("RGBA", size_ss, (0, 0, 0, 0)); d_pin = ImageDraw.Draw(pinl)
        sh_rgb = col.get("shadow", (0, 0, 0))

        # ---------------------------------------------------------------- línea de ruta
        if st["progress"] > 0:
            vs = vis * ss
            # sombra proyectada (1x)
            off = np.array(ln.get("shadow_offset", [0, 0])) * k
            draw_polyline(d_sh, vis + off, ln["width"] * k, sh_rgb + (int(255 * ln.get("shadow_opacity", 0)),))
            # glow (1x, se desenfoca después)
            if ln.get("glow_width", 0) > 0:
                draw_polyline(d_gl, vis, ln["glow_width"] * k, col["glow"] + (255,))
            # borde oscuro
            draw_polyline(d_ln, vs, ln["casing_width"] * k * ss, col["line_casing"] + (255,))
            # trazo principal con degradado a lo largo de la ruta (proyección inicio -> fin)
            p0, p1 = poly[0], poly[-1]
            e = p1 - p0
            ww = np.clip(((vis - p0) @ e) / max(float(e @ e), 1e-6), 0, 1)
            bright = col.get("line_bright", col["line"])
            colors = [lerp_rgb(col["line"], bright, float(w)) + (255,) for w in ww]
            draw_polyline(d_ln, vs, ln["width"] * k * ss, colors=colors)
            # núcleo luminoso (semitransparente, capa fx)
            if ln.get("core_width", 0) > 0 and ln.get("core_opacity", 0) > 0:
                draw_polyline(d_fx, vs, ln["core_width"] * k * ss,
                              col.get("line_core", (255, 255, 255)) + (int(255 * ln["core_opacity"]),))
            # flujo de guiones que avanza hacia el destino
            fl = ln.get("flow", {})
            if fl.get("enabled") and n_vis > 1:
                offset = fl.get("speed", 600) * t
                for i0, i1 in dash_runs(n_vis, self.spacing0, fl["dash"], fl["gap"], offset):
                    draw_polyline(d_fx, vs[i0:i1], fl["width"] * k * ss, col.get("flow", (255, 255, 255)) + (int(255 * fl["opacity"]),))
            # cometa en la punta mientras se dibuja
            hd = ln.get("head", {})
            if hd.get("enabled") and st["head_alpha"] > 0 and st["progress"] < 1 + 1e-9:
                hx, hy = vis[-1]
                gr = hd.get("glow_radius", 90) * k
                d_hg.ellipse([hx - gr, hy - gr, hx + gr, hy + gr], fill=col["glow"] + (255,))
                r = hd.get("radius", 30) * k * ss
                d_fx.ellipse([hx * ss - r, hy * ss - r, hx * ss + r, hy * ss + r],
                             fill=col.get("head", (255, 255, 255)) + (int(255 * st["head_alpha"]),))

        # ---------------------------------------------------------------- pin de ubicación
        if st["pin"] is not None:
            self._draw_pin(d_sh, d_pin, pinl, anchor, st, t)

        # ---------------------------------------------------------------- composición
        blur_bbox(shadow, max(ln.get("shadow_blur", 0), pin.get("shadow_blur", 0)) * k)
        blur_bbox(glow, ln.get("glow_blur", 0) * k)
        alpha_mul(glow, ln.get("glow_opacity", 0.4))
        blur_bbox(headglow, ln.get("head", {}).get("glow_radius", 90) * k * 0.5)
        alpha_mul(headglow, 0.9 * st["head_alpha"])
        if ss != 1:
            line = line.resize(size1, Image.LANCZOS)
            fx = fx.resize(size1, Image.LANCZOS)
            pinl = pinl.resize(size1, Image.LANCZOS)
        out = shadow
        for layer in (glow, line, headglow, fx, pinl):
            out = Image.alpha_composite(out, layer)
        return out

    def _draw_pin(self, d_sh, d_pin, pinl, anchor, st, t):
        cfg, k, ss, col = self.cfg, self.k, self.ss, self.colors
        pin = cfg["pin"]
        ps = st["pin"]
        sx, sy, hf = ps["scale_x"], ps["scale_y"], ps["height_frac"]
        ax, ay = float(anchor[0]), float(anchor[1])
        dy = ps["dy"] * k
        sh_rgb = col.get("shadow", (0, 0, 0))
        R, hgt, cw, hole = pin["head_radius"], pin["height"], pin.get("casing_width", 0), pin.get("hole_radius", 0)
        d = hgt - R                                        # punta -> centro de la cabeza

        # sombra en el suelo (1x): más grande y tenue cuanto más alto está el pin
        rx, ry = 68 * k * (1 + 0.6 * hf) * max(sx, 0.2), 26 * k * (1 + 0.6 * hf)
        op = pin.get("shadow_opacity", 0.35) * (1 - 0.7 * hf) * min(1.0, sx)
        d_sh.ellipse([ax - rx, ay - ry + 8 * k, ax + rx, ay + ry + 8 * k], fill=sh_rgb + (int(255 * op),))

        # ondas de pulso en el suelo (elipses: perspectiva del plano del suelo)
        rp = pin.get("ripple", {})
        if rp.get("enabled") and st["impact"] is not None:
            for i in range(int(rp.get("count", 2))):
                s0 = st["impact"] + i * rp.get("interval", 0.35)
                ru = (t - s0) / max(rp.get("duration", 1.0), 1e-6)
                if ru < 0 or ru >= 1:
                    continue
                e = ease_out_cubic(ru)
                rad = (rp["radius_from"] + (rp["radius_to"] - rp["radius_from"]) * e) * k * ss
                wdt = max(1, int(rp.get("width", 12) * (1 - 0.6 * ru) * k * ss))
                cx, cy = ax * ss, ay * ss
                d_pin.ellipse([cx - rad, cy - rad * 0.6, cx + rad, cy + rad * 0.6],
                              outline=col["ripple"] + (int(255 * rp.get("opacity", 0.75) * (1 - ru)),), width=wdt)

        def M(x, y):   # coords locales del pin (punta en 0,0; y hacia arriba negativo) -> capa ss
            return (ax * ss + x * sx * k * ss, (ay + dy) * ss + y * sy * k * ss)

        def teardrop(r):
            a = math.acos(r / d)
            tx, ty = r * math.sin(a), -d + r * math.cos(a)
            tip_y = 0.0 if r == R else d * r / R - d          # la punta del borde se alarga (curva offset)
            tri = [M(0, tip_y), M(tx, ty), M(-tx, ty)]
            cx, cy = M(0, -d)
            return tri, (cx, cy, r * sx * k * ss, r * sy * k * ss)

        # borde oscuro
        if cw > 0:
            tri, (cx, cy, erx, ery) = teardrop(R + cw)
            d_pin.polygon(tri, fill=col["pin_casing"] + (255,))
            d_pin.ellipse([cx - erx, cy - ery, cx + erx, cy + ery], fill=col["pin_casing"] + (255,))

        # cuerpo: degradado vertical + brillo especular, recortados por la silueta
        tri, (cx, cy, erx, ery) = teardrop(R)
        xs = [p[0] for p in tri] + [cx - erx, cx + erx]
        ys = [p[1] for p in tri] + [cy - ery, cy + ery]
        x0, y0 = int(math.floor(min(xs))) - 2, int(math.floor(min(ys))) - 2
        x1, y1 = int(math.ceil(max(xs))) + 3, int(math.ceil(max(ys))) + 3
        w, h = x1 - x0, y1 - y0
        mask = Image.new("L", (w, h), 0)
        dm = ImageDraw.Draw(mask)
        dm.polygon([(x - x0, y - y0) for x, y in tri], fill=255)
        dm.ellipse([cx - erx - x0, cy - ery - y0, cx + erx - x0, cy + ery - y0], fill=255)
        top, bot = np.array(col.get("pin_top", col.get("pin", (31, 227, 106)))), np.array(col.get("pin_bottom", col.get("pin", (31, 227, 106))))
        wgt = np.linspace(0, 1, h)[:, None]
        rgb = (top[None, :] * (1 - wgt) + bot[None, :] * wgt).astype(np.float32)   # (h, 3)
        arr = np.zeros((h, w, 4), np.uint8)
        rgbw = np.repeat(rgb[:, None, :], w, axis=1)
        hl = Image.new("L", (w, h), 0)
        dh = ImageDraw.Draw(hl)
        hx, hy = cx - x0 - 0.3 * erx, cy - y0 - 0.35 * ery
        dh.ellipse([hx - 0.5 * erx, hy - 0.42 * ery, hx + 0.5 * erx, hy + 0.42 * ery], fill=int(255 * 0.38))
        hl = hl.filter(ImageFilter.GaussianBlur(max(1.0, 0.22 * erx)))
        hla = (np.array(hl).astype(np.float32) / 255.0)[:, :, None]
        rgbw = rgbw + (255 - rgbw) * hla
        arr[:, :, :3] = np.clip(rgbw, 0, 255).astype(np.uint8)
        arr[:, :, 3] = np.array(mask)
        pinl.alpha_composite(Image.fromarray(arr, "RGBA"), (x0, y0))

        # hueco blanco
        if hole > 0:
            hrx, hry = hole * sx * k * ss, hole * sy * k * ss
            d_pin.ellipse([cx - hrx, cy - hry, cx + hrx, cy + hry], fill=col["pin_hole"] + (255,))

        # etiqueta (pastilla) a la derecha de la cabeza
        lab = pin.get("label", {})
        if lab.get("enabled") and lab.get("text") and st["impact"] is not None:
            lstart = st["impact"] + lab.get("delay", 0.15)
            if t >= lstart:
                lu = clamp01((t - lstart) / max(lab.get("duration", 0.35), 1e-6))
                e = ease_out_cubic(lu)
                slide = (1 - e) * 40 * k * ss
                text = lab["text"]
                bb = self.font.getbbox(text)
                tw, th = bb[2] - bb[0], bb[3] - bb[1]
                px, py = lab.get("padding_x", 36) * k * ss, lab.get("padding_y", 22) * k * ss
                bw, bh = tw + 2 * px, th + 2 * py
                lx = ax * ss + (R + cw + lab.get("gap", 30)) * k * ss - slide
                ly = (ay + dy) * ss - d * k * ss - bh / 2
                rad = min(lab.get("radius", 44) * k * ss, bh / 2)
                pill = Image.new("RGBA", (int(bw) + 4, int(bh) + 4), (0, 0, 0, 0))
                dp = ImageDraw.Draw(pill)
                dp.rounded_rectangle([0, 0, bw, bh], radius=rad, fill=col["label_bg"] + (int(255 * e),))
                dp.text((px - bb[0], py - bb[1]), text, font=self.font, fill=col["label_text"] + (int(255 * e),))
                pinl.alpha_composite(pill, (int(lx), int(ly)))
                d_sh.rounded_rectangle([lx / ss, ly / ss + 6 * k, (lx + bw) / ss, (ly + bh) / ss + 6 * k],
                                       radius=rad / ss, fill=sh_rgb + (int(255 * 0.3 * e),))

    def composite(self, frame_rgb, t):
        over = self.render_rgba(t)
        return Image.alpha_composite(frame_rgb.convert("RGBA"), over).convert("RGB")


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

    cfg_dir = os.path.dirname(os.path.abspath(args.config)) or "."
    cfg = json.load(open(args.config, encoding="utf-8"))
    tracking = None
    tpath = args.tracking or cfg.get("tracking")
    if tpath and not args.no_tracking:
        tpath = tpath if os.path.isabs(tpath) else os.path.join(cfg_dir, tpath)
        if os.path.exists(tpath):
            tracking = json.load(open(tpath))
        else:
            print(f"Aviso: no existe {tpath}; se renderiza con cámara fija (ejecuta track.py para anclar el pin).")

    W, H, fps, duration = probe(args.video)
    cfg["video"].update({"width": W, "height": H, "fps": fps})
    out_w, out_h = int(round(W * args.scale / 2) * 2), int(round(H * args.scale / 2) * 2)
    r = OverlayRenderer(cfg, tracking, out_w, out_h, supersample=max(1, args.ss), cfg_dir=cfg_dir)
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
