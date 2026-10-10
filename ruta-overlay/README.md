# Overlay de ruta animada + pin sobre video aéreo

Animación superpuesta (estilo navegación de Google Maps, acabado motion-graphics) sobre `video_mapa.mov`:

1. **Línea de ruta verde** que entra por el borde inferior, sube por el carril izquierdo de la avenida
   y termina sobre el edificio de techo blanco con paneles solares. Trazo con degradado, borde oscuro,
   brillo (glow), núcleo luminoso, "cometa" en la punta mientras se dibuja y un flujo de guiones que
   avanza hacia el destino.
2. **Pin de ubicación** del mismo verde que aparece en el fotograma exacto en que la línea llega:
   cae desde arriba, se aplasta al impactar y rebota; emite dos ondas de pulso sobre el suelo y
   despliega una etiqueta ("Tu destino", editable). Queda anclado al edificio aunque el dron avance.

Todo se controla desde un solo archivo (`route.json`); el movimiento de cámara se compensa con
`tracking.json`, generado automáticamente por `track.py`.

```
ruta-overlay/
├── route.json        ← ruta, colores, grosores, tiempos, etiqueta y puntos de rastreo (edita esto)
├── tracking.json     ← homografía por fotograma (ya generado para video_mapa.mov)
├── track.py          ← rastrea puntos fijos del terreno y genera tracking.json
├── render.py         ← hornea la animación en un MP4 / ProRes con alfa / secuencia PNG
├── overlay.html      ← previsualización en el navegador (video + SVG), scrubber fotograma a fotograma
├── overlay.js        ← lógica de la animación para la web (misma geometría y tiempos que render.py)
└── fonts/            ← DM Sans Bold (licencia OFL) para la etiqueta del pin
```

## Requisitos

- Python 3.9+ con `pillow` y `numpy`: `pip install pillow numpy`
- `ffmpeg` y `ffprobe` en el PATH (https://ffmpeg.org)
- Para la previsualización web: cualquier navegador moderno y un servidor estático local.

## Uso rápido (video final)

```bash
cd ruta-overlay
cp /ruta/a/video_mapa.mov .            # el video original (2160x3840, 30 fps, 4 s)

# 1) Rastreo del movimiento de cámara (solo una vez por video; ~20 s)
python3 track.py --video video_mapa.mov --config route.json --out tracking.json

# 2) Render del video final
python3 render.py --video video_mapa.mov --out ruta_overlay_1080.mp4 --scale 0.5   # 1080x1920
python3 render.py --video video_mapa.mov --out ruta_overlay_4k.mp4   --scale 1     # 2160x3840
```

El MP4 conserva el audio original. `--scale 0.5` tarda ~4 min; `--scale 1` unos 15–20 min
(añade `--ss 1` para acelerarlo a costa de un antialiasing más simple).

### Otras salidas

```bash
# Solo el overlay con canal alfa (ProRes 4444) para montarlo en Premiere, After Effects o DaVinci
python3 render.py --video video_mapa.mov --alpha overlay_alpha.mov --scale 1

# Secuencia PNG con alfa (overlay_0000.png, overlay_0001.png, ...)
python3 render.py --video video_mapa.mov --png-seq overlay_png --scale 1

# Un solo fotograma para revisar (t en segundos)
python3 render.py --video video_mapa.mov --frame 2.9 --out revision_2_9.png
```

En el editor de video, coloca el `.mov` con alfa (o la secuencia PNG) en una pista encima del video
original, alineado al fotograma 0 y con el mismo tamaño. No hace falta nada más: el overlay ya trae
compensado el movimiento de cámara.

## Previsualización en el navegador

```bash
# El .mov es HEVC; conviértelo a H.264 para que lo reproduzca cualquier navegador
ffmpeg -i video_mapa.mov -c:v libx264 -crf 18 -pix_fmt yuv420p -c:a aac video_mapa.mp4

python3 -m http.server 8000        # fetch() de los JSON necesita http://, no file://
# abre http://localhost:8000/overlay.html
```

- Espacio reproduce/pausa; `←` `→` avanzan un fotograma; el scrubber va fotograma a fotograma.
- Al hacer clic sobre el video se muestra la coordenada **nativa** (0–2160 × 0–3840) del punto:
  úsala para ajustar `route.json`.
- `overlay.js` es independiente del render: puedes incrustarlo en una landing page tal cual
  (un `<video>` + un `<svg>` encima, ver `overlay.html`).

## Línea de tiempo (valores por defecto)

| Instante | Qué pasa |
| --- | --- |
| 0.25 s | La línea empieza a entrar por el borde inferior; el cometa blanco marca la punta. |
| 0.25 → 2.60 s | Se dibuja con `easeInOutCubic` (arranque y llegada suaves); los guiones fluyen hacia el destino. |
| 2.60 s (fotograma 78) | La línea llega al edificio; el cometa se apaga y **aparece el pin** arriba, cayendo. |
| 2.90 s | Impacto: aplastamiento (squash), primera onda de pulso, sombra en el suelo. |
| 2.90 → 3.20 s | Rebote corto; a los 3.05 s se despliega la etiqueta. |
| 3.25 s | Segunda onda de pulso. |
| → 4.00 s | Pin y etiqueta anclados al edificio hasta el final. |

## Ajustar la animación (`route.json`)

| Clave | Qué controla |
| --- | --- |
| `route.points` | Puntos de control de la ruta, en píxeles del **fotograma 0** a resolución nativa. El primero queda justo fuera de pantalla (y > 3840) para que la línea "entre" por el borde inferior; el último es el edificio (punta del pin). Se suaviza con una spline Catmull-Rom: bastan 6–10 puntos. |
| `line.start` / `line.end` | Segundos en que la línea empieza y termina de dibujarse. El pin aparece exactamente en `line.end`. |
| `line.easing` | `easeInOutCubic` (suave), `easeOutCubic` o `linear`. |
| `line.width`, `casing_width`, `core_width`, `core_opacity` | Grosor del trazo, del borde oscuro y del núcleo luminoso (px nativos). |
| `line.glow_width`, `glow_blur`, `glow_opacity` | Brillo alrededor de la línea. Pon `glow_opacity: 0` para un look plano. |
| `line.shadow_*` | Sombra proyectada de la línea. |
| `line.head` | Cometa en la punta: `radius`, `glow_radius`, `fade` (segundos que tarda en apagarse al llegar). |
| `line.flow` | Guiones que avanzan: `dash`, `gap`, `speed` (px/s), `width`, `opacity`; `enabled: false` los quita. |
| `pin.animation` | `drop` (cae, se aplasta y rebota) o `pop` (escala con rebote desde la punta). |
| `pin.drop_height`, `fall_fraction`, `bounce_height`, `squash` | Ajuste fino de la caída: altura, parte de `duration` que dura la caída, altura del rebote y aplastamiento. |
| `pin.duration`, `overshoot`, `start_scale` | Duración total del pin y parámetros del modo `pop`. |
| `pin.height`, `head_radius`, `hole_radius`, `casing_width` | Geometría del pin (px nativos). |
| `pin.ripple` | Ondas de pulso: `count`, `interval`, `duration`, `radius_from/to`, `width`, `opacity`. |
| `pin.label` | Etiqueta: `text` (o `enabled: false`), `delay` tras el impacto, `font_size`, `padding_x/y`, `radius`, `gap`, `font`. |
| `colors.*` | `line` → `line_bright` (degradado del trazo), `line_casing`, `line_core`, `glow`, `head`, `flow`, `pin_top` → `pin_bottom`, `pin_casing`, `pin_hole`, `ripple`, `label_bg`, `label_text`, `shadow`. |
| `anchors` | Puntos fijos del terreno que rastrea `track.py` (ver abajo). |
| `tracking` | Nombre del archivo de rastreo; pon `null` si la cámara es fija. |

Después de cambiar `route.json` basta con volver a ejecutar `render.py` (o recargar `overlay.html`).
Solo hace falta repetir `track.py` si cambias los `anchors` o el video.

## Cómo funciona el anclaje (`track.py`)

El dron avanza durante los 4 s y el edificio se desplaza ~160 px en pantalla; un pin estático se
"despegaría". `track.py` sigue por correlación normalizada varios puntos fijos del terreno (los
`anchors` de `route.json`: los paneles solares, el letrero, esquinas de techos…) y ajusta una
**homografía por fotograma** que lleva las coordenadas del fotograma 0 a cada fotograma. La ruta y
el pin se definen una sola vez (fotograma 0) y se transforman con esa matriz, así el pin queda
pegado al edificio (error medio < 1 px con el ancla `paneles` ponderada con `weight: 25`).

Para usarlo con otro video:

1. Extrae el primer fotograma (`ffmpeg -i video.mov -frames:v 1 f0.png`) y elige 4–8 puntos con
   textura clara y que no se muevan (nada de coches, árboles ni sombras); dales más `weight` a los
   que estén junto al destino.
2. Escribe sus coordenadas en `anchors`, dibuja la ruta en `route.points` y ejecuta `track.py`.
   El script imprime el error de ajuste por punto; descarta los que superen ~15 px.
3. Revisa con `render.py --frame <t>` en el primer y el último fotograma.

Si la cámara está fija, salta este paso (`"tracking": null`).
