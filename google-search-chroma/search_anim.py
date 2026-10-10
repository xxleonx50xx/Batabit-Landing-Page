#!/usr/bin/env python3
"""
search_anim.py — Animación motion graphics de una búsqueda en Google sobre fondo chroma (azul #0000FF por defecto).

Secuencia: la barra aparece con scale-up desde el centro -> se escribe el texto letra a letra con cursor
parpadeante -> pausa, el cursor desaparece -> "Enter": la lupa rebota y se tiñe de azul Google y la barra da
un pequeño empuje hacia arriba -> la barra se desliza hacia arriba y se desvanece -> queda solo el fondo.

Motion blur real: cada fotograma en movimiento se compone promediando varios sub-fotogramas (supermuestreo
temporal, obturador de 180°). Antialiasing por supermuestreo espacial 2x.

Requisitos: Python 3.9+, Pillow, numpy y ffmpeg en el PATH.

Ejemplos:
    python3 search_anim.py --out busqueda_chroma_1080x1920.mp4                 # vertical 9:16, 60 fps
    python3 search_anim.py --size 1920x1080 --out busqueda_chroma_1920x1080.mp4  # horizontal 16:9
    python3 search_anim.py --prores busqueda_chroma_422hq.mov                    # ProRes 422 HQ (mejor para chroma key)
    python3 search_anim.py --alpha busqueda_alpha.mov                            # fondo transparente (ProRes 4444), sin chroma
    python3 search_anim.py --text "Donde está GreenMex?" --bg "#00FF00"          # otro texto / chroma verde
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
from PIL import Image, ImageDraw, ImageFilter, ImageFont


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
        fp = cfg.get("font", "fonts/Roboto-Regular.ttf")
        self.font_path = fp if os.path.isabs(fp) else os.path.join(cfg_dir, fp)
        if not os.path.exists(self.font_path):
            for alt in ("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", "C:/Windows/Fonts/arial.ttf",
                        "/Library/Fonts/Arial.ttf", "/System/Library/Fonts/Supplemental/Arial.ttf"):
                if os.path.exists(alt):
                    self.font_path = alt
                    break
        self._fonts = {}

        # --- línea de tiempo: la escritura marca el ritmo de todo lo demás
        tm = cfg["timing"]
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
        self.t_hold = self.t_enter + tm["enter"]
        self.t_exit = self.t_hold + tm["hold_after_enter"]
        self.t_gone = self.t_exit + tm["exit"]
        self.duration = self.t_gone + tm["tail"]
        self.nframes = int(math.ceil(self.duration * self.fps))

        # --- banda vertical donde ocurre todo (para no dibujar el lienzo completo en cada sub-fotograma)
        bar = cfg["bar"]
        cy = self.H * bar["center_y_ratio"]
        half = bar["height"] * self.k * 0.5 * 1.3
        sh = bar["shadow"]
        margin = (sh["blur"] * 3 + abs(sh["offset"][1]) + 20) * self.k
        self.band_y0 = int(max(0, cy - half - tm["exit_distance"] * self.k - tm["enter_nudge"] * self.k - margin))
        self.band_y1 = int(min(self.H, cy + half + margin))
        self.band_h = self.band_y1 - self.band_y0

    def font(self, px):
        px = max(4, int(round(px)))
        if px not in self._fonts:
            self._fonts[px] = ImageFont.truetype(self.font_path, px)
        return self._fonts[px]

    # --- estado de todos los elementos en el instante t
    def state(self, t):
        tm = self.cfg["timing"]
        st = {"visible": True, "scale": 1.0, "dy": 0.0, "opacity": 1.0, "chars": 0,
              "caret": False, "icon_scale": 1.0, "icon_mix": 0.0}
        if t < 0 or t >= self.t_gone:
            st["visible"] = False
            return st
        # aparición: scale-up con ligero sobreimpulso y fundido de entrada
        if t < tm["appear"]:
            u = t / tm["appear"]
            s0 = tm["appear_scale_from"]
            st["scale"] = s0 + (1 - s0) * ease_out_back(u, tm["appear_overshoot"])
            st["opacity"] = ease_out_cubic(clamp01(t / (tm["appear"] * 0.5)))
        # escritura
        st["chars"] = sum(1 for ct in self.char_times if ct <= t)
        # cursor: fijo mientras se teclea, parpadea en las pausas, desaparece antes del "Enter"
        caret_until = self.t_enter - tm.get("caret_hide_before_enter", 0.12)
        if tm["appear"] * 0.6 <= t < caret_until:
            last_key = max([ct for ct in self.char_times if ct <= t], default=self.t_typing_start - 10)
            if t - last_key < 0.45 and st["chars"] > 0:
                st["caret"] = True
            else:
                phase = ((t - tm["appear"]) % self.cfg["caret"]["blink"]) / self.cfg["caret"]["blink"]
                st["caret"] = phase < 0.5
        # "Enter": la lupa rebota y se tiñe; la barra da un empujón hacia arriba
        if self.t_enter <= t < self.t_hold:
            u = (t - self.t_enter) / tm["enter"]
            peak = tm["enter_icon_scale"] - 1
            if u < 0.38:
                st["icon_scale"] = 1 + peak * ease_out_cubic(u / 0.38)
            else:
                st["icon_scale"] = 1 + peak * (1 - ease_in_out_cubic((u - 0.38) / 0.62))
            st["icon_mix"] = ease_out_cubic(clamp01(u / 0.5))
            st["dy"] = -tm["enter_nudge"] * math.sin(math.pi * u)
        elif t >= self.t_hold:
            st["icon_mix"] = 1.0
        # salida: se desliza hacia arriba y/o se desvanece
        if t >= self.t_exit:
            u = clamp01((t - self.t_exit) / tm["exit"])
            style = tm.get("exit_style", "slide-fade")
            if "slide" in style:
                st["dy"] += -tm["exit_distance"] * ease_in_cubic(u)
            if "fade" in style:
                st["opacity"] *= 1 - ease_in_quad(u)
            if style == "slide":
                st["opacity"] *= 1 - ease_in_quad(clamp01((u - 0.7) / 0.3))
        return st

    def moving(self, t):
        a, b = self.state(t), self.state(t + 1.0 / self.fps)
        return (abs(a["scale"] - b["scale"]) > 1e-4 or abs(a["dy"] - b["dy"]) > 0.05
                or abs(a["opacity"] - b["opacity"]) > 0.002 or abs(a["icon_scale"] - b["icon_scale"]) > 1e-3)

    # --- dibujo de un sub-fotograma: devuelve la banda RGBA (1x) con la barra, iconos, texto y cursor
    def draw(self, t):
        cfg, k, ss = self.cfg, self.k, self.ss
        st = self.state(t)
        bw, bh = self.W, self.band_h
        layer = Image.new("RGBA", (bw * ss, bh * ss), (0, 0, 0, 0))
        if not st["visible"] or st["opacity"] <= 0:
            return layer.resize((bw, bh), Image.LANCZOS) if ss != 1 else layer
        d = ImageDraw.Draw(layer)
        bar, ic, ty, ca = cfg["bar"], cfg["icons"], cfg["typography"], cfg["caret"]
        sc = st["scale"]
        cx = self.W / 2.0
        cy = self.H * bar["center_y_ratio"] + st["dy"] * k - self.band_y0

        def T(x, y):   # coords de diseño relativas al centro de la barra -> píxeles de la capa (ss)
            return ((cx + x * k * sc) * ss, (cy + y * k * sc) * ss)

        u = k * sc * ss  # 1 unidad de diseño en píxeles de la capa
        hw, hh = bar["width"] / 2, bar["height"] / 2
        x0, y0 = T(-hw, -hh)
        x1, y1 = T(hw, hh)

        # sombra (se dibuja aparte a 1x, se desenfoca y se pone debajo)
        shadow = None
        if self.shadow_on:
            sh = bar["shadow"]
            shadow = Image.new("RGBA", (bw, bh), (0, 0, 0, 0))
            sd = ImageDraw.Draw(shadow)
            ox, oy = sh["offset"][0] * k * sc, sh["offset"][1] * k * sc
            sd.rounded_rectangle([x0 / ss + ox, y0 / ss + oy, x1 / ss + ox, y1 / ss + oy],
                                 radius=bar["radius"] * k * sc, fill=(0, 0, 0, int(255 * sh["opacity"])))
            pad = int(sh["blur"] * k * 3) + 4
            bb = shadow.getbbox()
            if bb:
                cx0, cy0 = max(0, bb[0] - pad), max(0, bb[1] - pad)
                cx1, cy1 = min(bw, bb[2] + pad), min(bh, bb[3] + pad)
                crop = shadow.crop((cx0, cy0, cx1, cy1)).filter(ImageFilter.GaussianBlur(sh["blur"] * k * sc))
                shadow.paste(crop, (cx0, cy0))

        # barra blanca
        d.rounded_rectangle([x0, y0, x1, y1], radius=bar["radius"] * u, fill=hex_rgb(bar["color"]) + (255,))

        # lupa (icono de 24 unidades de rejilla, como el SVG de Google)
        size = ic["size"]
        g = size / 24.0 * u * st["icon_scale"]            # 1 unidad de rejilla del icono
        mx, my = T(-hw + ic["padding"] + size / 2, 0)
        col = mix(hex_rgb(ic["search_color"]), hex_rgb(ic["search_active_color"]), st["icon_mix"]) + (255,)
        stroke = ic["stroke"] / 24.0 * size * u * st["icon_scale"] * 0.5
        ccx, ccy, cr = mx + (10.2 - 12) * g, my + (10.2 - 12) * g, 6.2 * g
        d.ellipse([ccx - cr, ccy - cr, ccx + cr, ccy + cr], outline=col, width=max(1, int(round(stroke))))
        hx0, hy0 = mx + (14.9 - 12) * g, my + (14.9 - 12) * g
        hx1, hy1 = mx + (20.6 - 12) * g, my + (20.6 - 12) * g
        d.line([(hx0, hy0), (hx1, hy1)], fill=col, width=max(1, int(round(stroke))))
        r2 = stroke / 2
        for px_, py_ in ((hx0, hy0), (hx1, hy1)):
            d.ellipse([px_ - r2, py_ - r2, px_ + r2, py_ + r2], fill=col)

        # texto escrito hasta el momento + cursor
        font = self.font(ty["size"] * u)
        tx, tyy = T(-hw + ic["padding"] + size + ty["gap_after_icon"], 0)
        typed = self.text[: st["chars"]]
        tcol = hex_rgb(ty["color"]) + (255,)
        if typed:
            d.text((tx, tyy), typed, font=font, fill=tcol, anchor="lm")
        if st["caret"]:
            cxp = tx + (font.getlength(typed) if typed else 0) + 2 * u
            cw, ch = ca["width"] * u, ca["height"] * u
            d.rectangle([cxp, tyy - ch / 2, cxp + cw, tyy + ch / 2], fill=tcol)

        # micrófono
        mcx, mcy = T(hw - ic["padding"] - size / 2, 0)
        g = size / 24.0 * u
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

        if ss != 1:
            layer = layer.resize((bw, bh), Image.LANCZOS)
        if st["opacity"] < 1:
            r_, g_, b_, a_ = layer.split()
            layer.putalpha(a_.point(lambda v: int(v * st["opacity"])))
            if shadow is not None:
                r_, g_, b_, a_ = shadow.split()
                shadow.putalpha(a_.point(lambda v: int(v * st["opacity"])))
        if shadow is not None:
            layer = Image.alpha_composite(shadow, layer)
        return layer

    # --- fotograma completo (con motion blur por supermuestreo temporal cuando hay movimiento)
    def frame_rgba(self, i):
        t0 = i / self.fps
        mb = self.cfg.get("motion_blur", {})
        n = int(mb.get("samples", 8)) if self.moving(t0) else 1
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


def encode(anim, path, codec_args, pix, rgba=False, label=""):
    fmt_pix = "rgba" if rgba else "rgb24"
    enc = subprocess.Popen(["ffmpeg", "-v", "error", "-y", "-f", "rawvideo", "-pix_fmt", fmt_pix,
                            "-s", f"{anim.W}x{anim.H}", "-r", str(anim.fps), "-i", "-"] + codec_args +
                           ["-pix_fmt", pix, "-r", str(anim.fps), path], stdin=subprocess.PIPE)
    for i in range(anim.nframes):
        if rgba:
            f = anim.frame_rgba(i)
            data = (np.clip(f, 0, 1) * 255 + 0.5).astype(np.uint8).tobytes()
        else:
            data = anim.frame_rgb(i).tobytes()
        enc.stdin.write(data)
        if i % 30 == 0:
            print(f"  {label}{i}/{anim.nframes}", flush=True)
    enc.stdin.close()
    enc.wait()
    if enc.returncode != 0:
        sys.exit(f"ffmpeg falló al escribir {path}")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--config", default=os.path.join(os.path.dirname(os.path.abspath(__file__)), "search.json"))
    ap.add_argument("--out", default=None, help="MP4 H.264 de salida (o PNG con --frame)")
    ap.add_argument("--prores", default=None, help="ProRes 422 HQ (.mov) con el fondo chroma: ideal para keying")
    ap.add_argument("--alpha", default=None, help="ProRes 4444 (.mov) con fondo transparente (sin chroma)")
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
          f"Enter {anim.t_enter:.2f} s · sale {anim.t_exit:.2f}-{anim.t_gone:.2f} s")

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

    if args.alpha:
        encode(anim, args.alpha, ["-c:v", "prores_ks", "-profile:v", "4444"], "yuva444p10le", rgba=True, label="alfa ")
        print(f"Listo: {args.alpha} (fondo transparente)")
    if args.prores:
        encode(anim, args.prores, ["-c:v", "prores_ks", "-profile:v", "3"], "yuv422p10le", label="prores ")
        print(f"Listo: {args.prores} (ProRes 422 HQ)")
    if args.out or not (args.alpha or args.prores):
        out = args.out or "busqueda_chroma.mp4"
        encode(anim, out, ["-c:v", "libx264", "-preset", "slow", "-crf", str(args.crf), "-movflags", "+faststart"],
               "yuv420p", label="mp4 ")
        print(f"Listo: {out}")


if __name__ == "__main__":
    main()
