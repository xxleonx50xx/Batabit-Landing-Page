#!/usr/bin/env python3
"""
search_anim.py — Animación motion graphics de una búsqueda en Google sobre fondo chroma (azul #0000FF por defecto).

Secuencia:
  1. La barra entra con un rebote elástico desde el centro (scale-up + leve subida) y los iconos aparecen en cascada.
  2. Se escribe el texto letra a letra con ritmo humano; cada letra "salta" al teclearse y el cursor parpadea en
     las pausas. A partir de la sexta letra se despliega un panel de sugerencias tipo Google.
  3. Pausa; el cursor desaparece y la primera sugerencia se resalta. "Enter": la barra se presiona y da un empujón,
     la lupa rebota, se tiñe de azul y emite una onda tipo Material, el panel se pliega y una línea de progreso azul
     recorre la barra.
  4. La barra sale disparada hacia arriba con estiramiento y fundido; queda solo el fondo.

Motion blur real: cada fotograma en movimiento se compone promediando sub-fotogramas (supermuestreo temporal,
obturador de 180°). Antialiasing por supermuestreo espacial 2x. Una leve flotación continua mantiene vivo el conjunto.

Requisitos: Python 3.9+, Pillow, numpy y ffmpeg en el PATH.

Ejemplos:
    python3 search_anim.py --out busqueda_chroma_1080x1920.mp4                 # vertical 9:16, 60 fps
    python3 search_anim.py --size 1920x1080 --out busqueda_chroma_1920x1080.mp4  # horizontal 16:9
    python3 search_anim.py --prores busqueda_chroma_422hq.mov                    # ProRes 422 HQ (mejor para chroma key)
    python3 search_anim.py --alpha busqueda_alpha.mov                            # fondo transparente (ProRes 4444), sin chroma
    python3 search_anim.py --text "¿Donde está GreenMex?" --bg "#00FF00"         # otro texto / chroma verde
    python3 search_anim.py --frame 2.4 --out revision.png                        # un fotograma para revisar
    python3 search_anim.py --contact hoja.png                                    # hoja de contactos (12 fotogramas)

Todos los valores de diseño (en unidades de un lienzo de 1080 px de ancho) viven en search.json.
"""
import argparse
import json
import math
import os
import random
import subprocess
import sys

import numpy as np
from PIL import Image, ImageChops, ImageDraw, ImageFilter, ImageFont


# ----------------------------------------------------------------------------- utilidades


def hex_rgb(h):
    h = h.lstrip("#")
    return tuple(int(h[i: i + 2], 16) for i in (0, 2, 4))


def mix(a, b, w):
    return tuple(int(round(a[i] + (b[i] - a[i]) * w)) for i in range(3))


def clamp01(x):
    return 0.0 if x < 0 else 1.0 if x > 1 else x


def ease_out_cubic(u):
    return 1 - (1 - u) ** 3


def ease_in_cubic(u):
    return u ** 3


def ease_in_quad(u):
    return u * u


def ease_in_out_cubic(u):
    return 4 * u ** 3 if u < 0.5 else 1 - (-2 * u + 2) ** 3 / 2


def ease_out_back(u, s=1.70158):
    c3 = s + 1
    return 1 + c3 * (u - 1) ** 3 + s * (u - 1) ** 2


def ease_out_elastic(u):
    if u <= 0:
        return 0.0
    if u >= 1:
        return 1.0
    c4 = (2 * math.pi) / 3
    return 2 ** (-10 * u) * math.sin((u * 10 - 0.75) * c4) + 1


def bump(u):
    """0 -> 1 -> 0 suave (seno) para pulsos."""
    return math.sin(math.pi * clamp01(u))


# ----------------------------------------------------------------------------- animación


class SearchAnimation:
    def __init__(self, cfg, cfg_dir, size=None, fps=None, text=None, bg=None, shadow=True):
        self.cfg = cfg
        self.W, self.H = size or cfg["size"]
        self.fps = fps or cfg["fps"]
        self.text = text if text is not None else cfg["text"]
        self.bg = hex_rgb(bg or cfg["background"])
        self.ss = int(cfg.get("supersample", 2))
        self.k = min(self.W, self.H) / 1080.0            # unidades de diseño -> píxeles de salida
        self.shadow_on = shadow and cfg["bar"]["shadow"]["opacity"] > 0
        self.font_path = self._font_file(cfg.get("font", "fonts/Roboto-Regular.ttf"), cfg_dir)
        self.font_medium_path = self._font_file(cfg.get("font_medium", cfg.get("font")), cfg_dir)
        self._fonts = {}

        # --- línea de tiempo: la escritura marca el ritmo de todo lo demás
        tm = cfg["timing"]
        dd = cfg.get("dropdown", {})
        rng = random.Random(tm.get("type_seed", 7))
        t = tm["appear"] + tm["idle_before_typing"]
        self.char_times = []
        for i, ch in enumerate(self.text):
            t += tm["type_interval"] + rng.uniform(-tm["type_jitter"], tm["type_jitter"])
            if i > 0 and self.text[i - 1] == " ":
                t += tm["type_space_pause"]
            if ch.isupper() or ch in "¿?¡!":
                t += 0.04
            self.char_times.append(t)
        self.t_typing_start = tm["appear"] + tm["idle_before_typing"]
        self.t_typed = self.char_times[-1] if self.char_times else self.t_typing_start
        self.t_enter = self.t_typed + tm["pause_after_typing"]
        self.t_enter_end = self.t_enter + tm["enter"]
        self.t_progress = self.t_enter + tm.get("progress_delay", 0.12)
        self.t_progress_end = self.t_progress + tm.get("progress", 0.55)
        self.t_exit = max(self.t_enter_end, self.t_progress_end) + tm["hold_after_enter"]
        self.t_gone = self.t_exit + tm["exit"]
        self.duration = self.t_gone + tm["tail"]
        self.nframes = int(math.ceil(self.duration * self.fps))
        n_after = int(dd.get("after_chars", 6))
        self.dropdown_on = bool(dd.get("enabled", True)) and len(self.char_times) >= n_after
        self.t_drop = self.char_times[n_after - 1] if self.dropdown_on else None
        self.n_rows = 1 + len(dd.get("suggestions", [])) if self.dropdown_on else 0
        self.panel_h = (dd.get("padding_top", 30) + self.n_rows * dd.get("row_height", 72)
                        + dd.get("padding_bottom", 16)) if self.dropdown_on else 0

        # --- banda vertical donde ocurre todo (para no dibujar el lienzo completo en cada sub-fotograma)
        bar = cfg["bar"]
        cy = self.H * bar["center_y_ratio"]
        half = bar["height"] * self.k * 0.5 * 1.4
        sh = bar["shadow"]
        margin = (sh["blur"] * 3 + abs(sh["offset"][1]) + 24) * self.k
        rise = (tm["exit_distance"] + tm["enter_nudge"] + tm.get("appear_rise", 0) + tm.get("idle_float", 0)) * self.k
        self.band_y0 = int(max(0, cy - half - rise - margin))
        self.band_y1 = int(min(self.H, cy + half + self.panel_h * self.k + tm.get("appear_rise", 0) * self.k + margin))
        self.band_h = self.band_y1 - self.band_y0

    @staticmethod
    def _font_file(rel, cfg_dir):
        p = rel if os.path.isabs(rel) else os.path.join(cfg_dir, rel)
        if os.path.exists(p):
            return p
        for alt in ("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", "C:/Windows/Fonts/arial.ttf",
                    "/Library/Fonts/Arial.ttf", "/System/Library/Fonts/Supplemental/Arial.ttf"):
            if os.path.exists(alt):
                return alt
        return None

    def font(self, px, medium=False):
        px = max(4, int(round(px)))
        key = (px, medium)
        if key not in self._fonts:
            path = self.font_medium_path if medium else self.font_path
            self._fonts[key] = ImageFont.truetype(path, px) if path else ImageFont.load_default()
        return self._fonts[key]

    # ------------------------------------------------------------------ estado en el instante t
    def state(self, t):
        tm = self.cfg["timing"]
        dd = self.cfg.get("dropdown", {})
        st = {"visible": True, "sx": 1.0, "sy": 1.0, "dy": 0.0, "float": 0.0, "opacity": 1.0, "chars": 0,
              "caret": False, "caret_pulse": 0.0, "icon_scale": 1.0, "icon_mix": 0.0,
              "icon_pop": [1.0, 1.0], "press": 0.0, "ripple": None, "progress": None,
              "panel": 0.0, "panel_rows": [], "hover": 0.0}
        if t < 0 or t >= self.t_gone:
            st["visible"] = False
            return st

        # aparición: rebote elástico desde el centro + leve subida; los iconos aparecen en cascada
        if t < tm["appear"]:
            u = t / tm["appear"]
            s0 = tm["appear_scale_from"]
            sc = s0 + (1 - s0) * ease_out_back(u, tm["appear_overshoot"])
            st["sx"] = st["sy"] = sc
            st["dy"] += tm.get("appear_rise", 0) * (1 - ease_out_cubic(u))
            st["opacity"] = ease_out_cubic(clamp01(t / (tm["appear"] * 0.45)))
        for j, delay in enumerate(tm.get("icons_pop_delay", [0.2, 0.32])):
            st["icon_pop"][j] = ease_out_back(clamp01((t - delay) / tm.get("icons_pop", 0.34)), 1.9) if t >= delay else 0.0

        # flotación continua muy sutil (se activa tras la aparición)
        amp = tm.get("idle_float", 0) * clamp01((t - tm["appear"]) / 0.6)
        st["float"] = amp * math.sin(2 * math.pi * t / tm.get("idle_float_period", 2.4))

        # escritura
        st["chars"] = sum(1 for ct in self.char_times if ct <= t)
        last_key = max([ct for ct in self.char_times if ct <= t], default=None)
        st["caret_pulse"] = bump((t - last_key) / 0.12) if last_key is not None and t - last_key < 0.12 else 0.0
        caret_until = self.t_enter - tm.get("caret_hide_before_enter", 0.12)
        if tm["appear"] * 0.6 <= t < caret_until:
            if last_key is not None and t - last_key < 0.45:
                st["caret"] = True
            else:
                phase = ((t - tm["appear"]) % self.cfg["caret"]["blink"]) / self.cfg["caret"]["blink"]
                st["caret"] = phase < 0.5

        # panel de sugerencias: se abre al teclear, se resalta la primera fila y se pliega con el Enter
        if self.dropdown_on and t >= self.t_drop:
            if t < self.t_enter:
                st["panel"] = ease_out_cubic(clamp01((t - self.t_drop) / dd.get("open", 0.32)))
            else:
                st["panel"] = 1 - ease_in_cubic(clamp01((t - self.t_enter) / dd.get("close", 0.22)))
            st["panel_rows"] = [ease_out_cubic(clamp01((t - self.t_drop - j * dd.get("stagger", 0.05)) / 0.28))
                                for j in range(self.n_rows)]
            h0 = self.t_enter - dd.get("hover_before_enter", 0.22)
            if t >= h0:
                st["hover"] = ease_out_cubic(clamp01((t - h0) / 0.12))

        # "Enter": presión + empujón de la barra, rebote y tinte de la lupa, onda y barra de progreso
        if self.t_enter <= t < self.t_enter_end:
            u = (t - self.t_enter) / tm["enter"]
            peak = tm["enter_icon_scale"] - 1
            if u < 0.35:
                st["icon_scale"] = 1 + peak * ease_out_cubic(u / 0.35)
            else:
                st["icon_scale"] = 1 + peak * (1 - ease_in_out_cubic((u - 0.35) / 0.65))
            st["icon_mix"] = ease_out_cubic(clamp01(u / 0.4))
            st["press"] = tm.get("enter_press", 0.04) * bump(u / 0.6) if u < 0.6 else 0.0
            st["dy"] += -tm["enter_nudge"] * bump(u)
        elif t >= self.t_enter_end:
            st["icon_mix"] = 1.0
        if self.t_enter <= t < self.t_enter + tm.get("ripple", 0.5):
            st["ripple"] = clamp01((t - self.t_enter) / tm.get("ripple", 0.5))
        if self.t_progress <= t < self.t_progress_end:
            st["progress"] = clamp01((t - self.t_progress) / tm.get("progress", 0.55))

        # salida: sale disparada hacia arriba con estiramiento y fundido
        if t >= self.t_exit:
            u = clamp01((t - self.t_exit) / tm["exit"])
            style = tm.get("exit_style", "slide-fade")
            if "slide" in style:
                st["dy"] += -tm["exit_distance"] * ease_in_cubic(u)
                stretch = tm.get("exit_stretch", 0.14) * bump(u)
                st["sy"] *= 1 + stretch
                st["sx"] *= 1 - stretch * 0.5
            if "fade" in style:
                st["opacity"] *= 1 - ease_in_quad(u)
            if style == "slide":
                st["opacity"] *= 1 - ease_in_quad(clamp01((u - 0.7) / 0.3))
        return st

    def motion(self, t):
        """0 = quieto, 1 = movimiento pequeño (teclas, panel), 2 = movimiento grande (entrada, Enter, salida)."""
        a, b = self.state(t), self.state(t + 1.0 / self.fps)
        big = (abs(a["sx"] - b["sx"]) > 1e-4 or abs(a["sy"] - b["sy"]) > 1e-4 or abs(a["dy"] - b["dy"]) > 0.05
               or abs(a["opacity"] - b["opacity"]) > 0.002 or abs(a["icon_scale"] - b["icon_scale"]) > 1e-3
               or a["ripple"] is not None or a["progress"] is not None or abs(a["press"] - b["press"]) > 1e-4)
        if big:
            return 2
        small = (a["chars"] != b["chars"] or a["caret_pulse"] > 0 or abs(a["panel"] - b["panel"]) > 1e-3
                 or a["icon_pop"] != b["icon_pop"] or abs(a["hover"] - b["hover"]) > 1e-3
                 or any(abs(x - y) > 1e-3 for x, y in zip(a["panel_rows"], b["panel_rows"]))
                 or any(t - ct < self.cfg["timing"].get("char_pop", 0.11) for ct in self.char_times if ct <= t))
        return 1 if small else 0

    # ------------------------------------------------------------------ dibujo de un sub-fotograma
    def _magnifier(self, d, cx, cy, g, color, stroke):
        """Lupa en una rejilla de 24 unidades (g = píxeles por unidad), centrada en (cx, cy)."""
        w = max(1, int(round(stroke)))
        ccx, ccy, cr = cx + (10.2 - 12) * g, cy + (10.2 - 12) * g, 6.2 * g
        d.ellipse([ccx - cr, ccy - cr, ccx + cr, ccy + cr], outline=color, width=w)
        hx0, hy0 = cx + (14.9 - 12) * g, cy + (14.9 - 12) * g
        hx1, hy1 = cx + (20.6 - 12) * g, cy + (20.6 - 12) * g
        d.line([(hx0, hy0), (hx1, hy1)], fill=color, width=w)
        r2 = stroke / 2
        for px_, py_ in ((hx0, hy0), (hx1, hy1)):
            d.ellipse([px_ - r2, py_ - r2, px_ + r2, py_ + r2], fill=color)

    def draw(self, t):
        cfg, k, ss = self.cfg, self.k, self.ss
        st = self.state(t)
        bw, bh = self.W, self.band_h
        layer = Image.new("RGBA", (bw * ss, bh * ss), (0, 0, 0, 0))
        if not st["visible"] or st["opacity"] <= 0:
            return layer.resize((bw, bh), Image.LANCZOS) if ss != 1 else layer
        d = ImageDraw.Draw(layer)
        bar, ic, ty, ca, dd, tm = cfg["bar"], cfg["icons"], cfg["typography"], cfg["caret"], cfg.get("dropdown", {}), cfg["timing"]
        sx, sy = st["sx"] * (1 - st["press"]), st["sy"] * (1 - st["press"])
        cx = self.W / 2.0
        cy = self.H * bar["center_y_ratio"] + (st["dy"] + st["float"]) * k - self.band_y0

        def T(x, y):   # coords de diseño relativas al centro de la barra -> píxeles de la capa (ss)
            return ((cx + x * k * sx) * ss, (cy + y * k * sy) * ss)

        ux, uy = k * sx * ss, k * sy * ss      # 1 unidad de diseño en píxeles de la capa (por eje)
        u = k * ss * (sx + sy) / 2
        hw, hh = bar["width"] / 2, bar["height"] / 2
        x0, y0 = T(-hw, -hh)
        x1, y1 = T(hw, hh)
        white = hex_rgb(bar["color"]) + (255,)
        shadow = Image.new("RGBA", (bw, bh), (0, 0, 0, 0)) if self.shadow_on else None
        sd = ImageDraw.Draw(shadow) if shadow else None
        sh = bar["shadow"]
        shadow_fill = (0, 0, 0, int(255 * sh["opacity"]))
        ox, oy = sh["offset"][0] * k * sx, sh["offset"][1] * k * sy

        # ---- panel de sugerencias (debajo de la barra, se pliega/despliega en altura)
        if st["panel"] > 0.001 and self.n_rows:
            ph = self.panel_h * st["panel"]                              # altura visible (unidades de diseño)
            px0, py0 = T(-hw + 10, hh - 30)
            px1, py1 = T(hw - 10, hh - 30 + ph)
            pr = min(dd.get("radius", 30) * u, (py1 - py0) / 2)
            if sd:
                sd.rounded_rectangle([px0 / ss + ox, py0 / ss + oy, px1 / ss + ox, py1 / ss + oy], radius=pr / ss, fill=shadow_fill)
            d.rounded_rectangle([px0, py0, px1, py1], radius=pr, fill=white)
            rows = Image.new("RGBA", layer.size, (0, 0, 0, 0))
            rd = ImageDraw.Draw(rows)
            rh = dd.get("row_height", 72)
            top = hh - 30 + dd.get("padding_top", 30)
            rfont = self.font(dd.get("font_size", 36) * u)
            mfont = self.font(dd.get("font_size", 36) * u, medium=True)
            tcol = hex_rgb(dd.get("text_color", "#202124"))
            icol = hex_rgb(dd.get("icon_color", "#9AA0A6")) + (255,)
            typed = self.text[: st["chars"]]
            items = [None] + list(dd.get("suggestions", []))
            for j, item in enumerate(items[: self.n_rows]):
                a = st["panel_rows"][j] if j < len(st["panel_rows"]) else 1.0
                if a <= 0:
                    continue
                ry = top + j * rh + rh / 2 + 14 * (1 - a)
                if j == 0 and st["hover"] > 0:
                    hx0, hy0 = T(-hw + 10, ry - rh / 2)
                    hx1, hy1 = T(hw - 10, ry + rh / 2)
                    hov = hex_rgb(dd.get("hover_color", "#F1F3F4")) + (int(255 * st["hover"]),)
                    rd.rectangle([hx0, hy0, hx1, hy1], fill=hov)
                alpha = int(255 * a)
                icx, icy = T(-hw + ic["padding"] + 12, ry)
                self._magnifier(rd, icx, icy, 26 / 24.0 * u, icol[:3] + (alpha,), 2.4 / 24.0 * 26 * u * 0.5)
                tx, tyy = T(-hw + ic["padding"] + ic["size"] + ty["gap_after_icon"], ry)
                if j == 0:
                    rd.text((tx, tyy), typed, font=rfont, fill=tcol + (alpha,), anchor="lm")
                    rest = self.text[st["chars"]:]
                    if rest:
                        rd.text((tx + rfont.getlength(typed), tyy), rest, font=mfont, fill=tcol + (alpha,), anchor="lm")
                else:
                    rd.text((tx, tyy), item, font=rfont, fill=tcol + (alpha,), anchor="lm")
            # recorte de las filas a la zona visible del panel
            clip = Image.new("L", layer.size, 0)
            ImageDraw.Draw(clip).rounded_rectangle([px0, py0, px1, py1], radius=pr, fill=255)
            rows.putalpha(ImageChops.multiply(rows.split()[3], clip))
            layer.alpha_composite(rows)

        # ---- barra blanca (encima del panel)
        if sd:
            sd.rounded_rectangle([x0 / ss + ox, y0 / ss + oy, x1 / ss + ox, y1 / ss + oy], radius=bar["radius"] * k * sy, fill=shadow_fill)
        d.rounded_rectangle([x0, y0, x1, y1], radius=bar["radius"] * uy, fill=white)

        # ---- efectos recortados a la barra: onda Material y barra de progreso
        if st["ripple"] is not None or st["progress"] is not None:
            fx = Image.new("RGBA", layer.size, (0, 0, 0, 0))
            fd = ImageDraw.Draw(fx)
            accent = hex_rgb(dd.get("accent_color", ic["search_active_color"]))
            if st["ripple"] is not None:
                r = st["ripple"]
                mx, my = T(-hw + ic["padding"] + ic["size"] / 2, 0)
                rad = (20 + 200 * ease_out_cubic(r)) * u
                fd.ellipse([mx - rad, my - rad, mx + rad, my + rad], fill=accent + (int(255 * 0.14 * (1 - r)),))
                ring = max(1, int(round((10 * (1 - r) + 2) * u)))
                fd.ellipse([mx - rad, my - rad, mx + rad, my + rad], outline=accent + (int(255 * 0.45 * (1 - r)),), width=ring)
            if st["progress"] is not None:
                p = ease_in_out_cubic(st["progress"])
                seg = bar["width"] * 0.38
                sx0 = -hw - seg + (bar["width"] + seg) * p
                lx0, ly0 = T(sx0, hh - 7)
                lx1, ly1 = T(sx0 + seg, hh)
                fd.rounded_rectangle([lx0, ly0, lx1, ly1], radius=3 * u, fill=accent + (255,))
            clip = Image.new("L", layer.size, 0)
            ImageDraw.Draw(clip).rounded_rectangle([x0, y0, x1, y1], radius=bar["radius"] * uy, fill=255)
            fx.putalpha(ImageChops.multiply(fx.split()[3], clip))
            layer.alpha_composite(fx)

        # ---- lupa (aparece en cascada, rebota y se tiñe con el Enter)
        size = ic["size"]
        isc = st["icon_scale"] * st["icon_pop"][0]
        if isc > 0.01:
            g = size / 24.0 * u * isc
            mx, my = T(-hw + ic["padding"] + size / 2, 0)
            col = mix(hex_rgb(ic["search_color"]), hex_rgb(ic["search_active_color"]), st["icon_mix"]) + (255,)
            self._magnifier(d, mx, my, g, col, ic["stroke"] / 24.0 * size * u * isc * 0.5)

        # ---- texto letra a letra (cada letra salta al aparecer) + cursor
        fs = ty["size"] * u
        font = self.font(fs)
        asc, desc = font.getmetrics()
        tx, tyy = T(-hw + ic["padding"] + size + ty["gap_after_icon"], 0)
        baseline = tyy + (asc - desc) / 2
        tcol = hex_rgb(ty["color"]) + (255,)
        for i in range(st["chars"]):
            age = t - self.char_times[i]
            xi = tx + font.getlength(self.text[:i])
            if age < tm.get("char_pop", 0.11):
                p = age / tm.get("char_pop", 0.11)
                scl = 0.45 + 0.55 * ease_out_back(p, 2.2)
                rise = tm.get("char_rise", 10) * u * (1 - ease_out_cubic(p))
                f2 = self.font(fs * max(0.3, min(1.25, round(scl * 20) / 20)))
                d.text((xi, baseline + rise), self.text[i], font=f2, fill=tcol, anchor="ls")
            else:
                d.text((xi, baseline), self.text[i], font=font, fill=tcol, anchor="ls")
        if st["caret"]:
            cxp = tx + (font.getlength(self.text[: st["chars"]]) if st["chars"] else 0) + 2 * u
            cw = ca["width"] * u
            ch = ca["height"] * u * (1 + 0.18 * st["caret_pulse"])
            d.rectangle([cxp, tyy - ch / 2, cxp + cw, tyy + ch / 2], fill=tcol)

        # ---- micrófono (aparece en cascada)
        mpop = st["icon_pop"][1]
        if mpop > 0.01:
            mcx, mcy = T(hw - ic["padding"] - size / 2, 0)
            g = size / 24.0 * u * mpop
            if ic.get("mic_style", "google") == "google":
                mc = {key: hex_rgb(v) + (255,) for key, v in ic["mic_colors"].items()}
            else:
                gray = hex_rgb(ic.get("mic_gray", "#9AA0A6")) + (255,)
                mc = {"capsule": gray, "stem": gray, "left": gray, "right": gray}
            d.rounded_rectangle([mcx - 3 * g, mcy - 10 * g, mcx + 3 * g, mcy + 3 * g], radius=3 * g, fill=mc["capsule"])
            d.rectangle([mcx - 1 * g, mcy + 6 * g, mcx + 1 * g, mcy + 10 * g], fill=mc["stem"])
            aw = max(1, int(round(2 * g)))
            d.arc([mcx - 6 * g, mcy - 6 * g, mcx + 6 * g, mcy + 6 * g], start=130, end=180, fill=mc["left"], width=aw)
            d.arc([mcx - 6 * g, mcy - 6 * g, mcx + 6 * g, mcy + 6 * g], start=0, end=130, fill=mc["right"], width=aw)

        # ---- composición: sombra desenfocada debajo, opacidad global
        if ss != 1:
            layer = layer.resize((bw, bh), Image.LANCZOS)
        if shadow is not None:
            bb = shadow.getbbox()
            if bb:
                pad = int(sh["blur"] * k * 3) + 4
                cx0, cy0 = max(0, bb[0] - pad), max(0, bb[1] - pad)
                cx1, cy1 = min(bw, bb[2] + pad), min(bh, bb[3] + pad)
                crop = shadow.crop((cx0, cy0, cx1, cy1)).filter(ImageFilter.GaussianBlur(sh["blur"] * k * (sx + sy) / 2))
                shadow.paste(crop, (cx0, cy0))
            layer = Image.alpha_composite(shadow, layer)
        if st["opacity"] < 1:
            r_, g_, b_, a_ = layer.split()
            layer.putalpha(a_.point(lambda v: int(v * st["opacity"])))
        return layer

    # ------------------------------------------------------------------ fotograma completo con motion blur
    def frame_rgba(self, i):
        t0 = i / self.fps
        mb = self.cfg.get("motion_blur", {})
        m = self.motion(t0)
        n = int(mb.get("samples", 8)) if m == 2 else int(mb.get("samples_small", 4)) if m == 1 else 1
        shutter = float(mb.get("shutter", 0.5))
        if n <= 1:
            band = np.asarray(self.draw(t0)).astype(np.float32) / 255.0
        else:
            acc = None
            for j in range(n):
                ts = t0 + (j + 0.5) / n * shutter / self.fps
                a = np.asarray(self.draw(ts)).astype(np.float32) / 255.0
                prem = np.concatenate([a[:, :, :3] * a[:, :, 3:4], a[:, :, 3:4]], axis=2)
                acc = prem if acc is None else acc + prem
            acc /= n
            alpha = acc[:, :, 3:4]
            rgb = np.where(alpha > 1e-6, acc[:, :, :3] / np.maximum(alpha, 1e-6), 0)
            band = np.concatenate([rgb, alpha], axis=2)
        full = np.zeros((self.H, self.W, 4), np.float32)
        full[self.band_y0: self.band_y1] = band
        return full

    def frame_rgb(self, i):
        f = self.frame_rgba(i)
        a = f[:, :, 3:4]
        bg = np.array(self.bg, np.float32) / 255.0
        out = f[:, :, :3] * a + bg * (1 - a)
        return (np.clip(out, 0, 1) * 255 + 0.5).astype(np.uint8)

    def chroma_check(self, i):
        """Cuenta píxeles de los elementos (alfa > 0) que coinciden exactamente con el color de fondo."""
        f = self.frame_rgba(i)
        rgb = self.frame_rgb(i)
        mask = f[:, :, 3] > 0.02
        same = np.all(rgb == np.array(self.bg, np.uint8), axis=2) & mask
        return int(same.sum()), int(mask.sum())


# ----------------------------------------------------------------------------- salida


def encode_all(anim, sinks):
    """Renderiza cada fotograma UNA sola vez y lo envía a todas las salidas (MP4, ProRes, alfa) a la vez.

    sinks: lista de dicts {path, codec: [args ffmpeg], pix: formato de píxel de salida, rgba: bool}
    """
    procs = []
    for s in sinks:
        fmt_pix = "rgba" if s["rgba"] else "rgb24"
        procs.append(subprocess.Popen(
            ["ffmpeg", "-v", "error", "-y", "-f", "rawvideo", "-pix_fmt", fmt_pix,
             "-s", f"{anim.W}x{anim.H}", "-r", str(anim.fps), "-i", "-"] + s["codec"] +
            ["-pix_fmt", s["pix"], "-r", str(anim.fps), s["path"]], stdin=subprocess.PIPE))
    bg = np.array(anim.bg, np.float32) / 255.0
    need_rgb = any(not s["rgba"] for s in sinks)
    for i in range(anim.nframes):
        f = anim.frame_rgba(i)
        data_rgba = (np.clip(f, 0, 1) * 255 + 0.5).astype(np.uint8).tobytes() if any(s["rgba"] for s in sinks) else None
        data_rgb = None
        if need_rgb:
            a = f[:, :, 3:4]
            data_rgb = (np.clip(f[:, :, :3] * a + bg * (1 - a), 0, 1) * 255 + 0.5).astype(np.uint8).tobytes()
        for s, p in zip(sinks, procs):
            p.stdin.write(data_rgba if s["rgba"] else data_rgb)
        if i % 30 == 0:
            print(f"  fotograma {i}/{anim.nframes}", flush=True)
    for s, p in zip(sinks, procs):
        p.stdin.close()
        p.wait()
        if p.returncode != 0:
            sys.exit(f"ffmpeg falló al escribir {s['path']}")
        print(f"Listo: {s['path']}")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--config", default=os.path.join(os.path.dirname(os.path.abspath(__file__)), "search.json"))
    ap.add_argument("--out", default=None, help="MP4 H.264 de salida (o PNG con --frame)")
    ap.add_argument("--prores", default=None, help="ProRes 422 HQ (.mov) con el fondo chroma: ideal para keying")
    ap.add_argument("--alpha", default=None, help="fondo transparente (.mov) sin chroma; códec según --alpha-codec")
    ap.add_argument("--alpha-codec", choices=["prores", "animation"], default="prores",
                    help="prores = ProRes 4444 (10 bits, ~45 MB); animation = QuickTime Animation RGBA, sin pérdida y mucho más ligero para gráficos planos")
    ap.add_argument("--size", default=None, help="ANCHOxALTO, p. ej. 1920x1080 (por defecto el de search.json)")
    ap.add_argument("--fps", type=int, default=None)
    ap.add_argument("--text", default=None, help="texto que se escribe en la barra")
    ap.add_argument("--bg", default=None, help="color de fondo chroma, p. ej. #0000FF o #00FF00")
    ap.add_argument("--no-shadow", action="store_true", help="sin sombra bajo la barra (si el keyer se la come)")
    ap.add_argument("--crf", type=int, default=14, help="calidad H.264 (menor = mejor)")
    ap.add_argument("--frame", type=float, default=None, help="renderiza solo este instante (s) a PNG")
    ap.add_argument("--contact", default=None, help="guarda una hoja de contactos PNG con 12 fotogramas")
    args = ap.parse_args()

    cfg = json.load(open(args.config, encoding="utf-8"))
    cfg_dir = os.path.dirname(os.path.abspath(args.config))
    size = tuple(int(v) for v in args.size.lower().split("x")) if args.size else None
    anim = SearchAnimation(cfg, cfg_dir, size=size, fps=args.fps, text=args.text, bg=args.bg, shadow=not args.no_shadow)

    print(f"{anim.W}x{anim.H} @ {anim.fps} fps · {anim.nframes} fotogramas · {anim.duration:.2f} s · fondo {args.bg or cfg['background']}")
    print(f"  aparece 0.00-{cfg['timing']['appear']:.2f} s · escribe {anim.t_typing_start:.2f}-{anim.t_typed:.2f} s · "
          f"sugerencias desde {anim.t_drop if anim.t_drop is not None else float('nan'):.2f} s · Enter {anim.t_enter:.2f} s · "
          f"progreso {anim.t_progress:.2f}-{anim.t_progress_end:.2f} s · sale {anim.t_exit:.2f}-{anim.t_gone:.2f} s")

    if args.frame is not None:
        out = args.out or f"frame_{args.frame:.2f}.png"
        Image.fromarray(anim.frame_rgb(int(round(args.frame * anim.fps)))).save(out)
        print(f"Fotograma t={args.frame}s guardado en {out}")
        return

    if args.contact:
        cols, rows = 6, 2
        thumb_w = 320
        thumb_h = int(thumb_w * anim.H / anim.W)
        sheet = Image.new("RGB", (cols * thumb_w, rows * thumb_h), (20, 20, 20))
        for n in range(cols * rows):
            i = int(n * (anim.nframes - 1) / (cols * rows - 1))
            im = Image.fromarray(anim.frame_rgb(i)).resize((thumb_w, thumb_h), Image.LANCZOS)
            sheet.paste(im, ((n % cols) * thumb_w, (n // cols) * thumb_h))
        sheet.save(args.contact)
        print(f"Hoja de contactos guardada en {args.contact}")
        return

    same, total = anim.chroma_check(int(round(anim.t_typed * anim.fps)))
    print(f"  chroma check: {same} de {total} píxeles de los elementos coinciden con el fondo exacto")

    sinks = []
    if args.alpha:
        if args.alpha_codec == "animation":
            sinks.append({"path": args.alpha, "codec": ["-c:v", "qtrle"], "pix": "argb", "rgba": True})
        else:
            sinks.append({"path": args.alpha, "codec": ["-c:v", "prores_ks", "-profile:v", "4444"], "pix": "yuva444p10le", "rgba": True})
    if args.prores:
        sinks.append({"path": args.prores, "codec": ["-c:v", "prores_ks", "-profile:v", "3"], "pix": "yuv422p10le", "rgba": False})
    if args.out or not sinks:
        sinks.append({"path": args.out or "busqueda_chroma.mp4", "rgba": False, "pix": "yuv420p",
                      "codec": ["-c:v", "libx264", "-preset", "slow", "-crf", str(args.crf), "-movflags", "+faststart"]})
    print("  salidas: " + ", ".join(s["path"] for s in sinks) + " (un solo pase de render)")
    encode_all(anim, sinks)


if __name__ == "__main__":
    main()
