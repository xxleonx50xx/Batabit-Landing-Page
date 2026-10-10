# Overlay de ruta animada + pin sobre video aéreo

Animación superpuesta (estilo navegación de Google Maps) sobre `video_mapa.mov`:

1. **Línea de ruta verde** que se dibuja progresivamente desde el borde inferior, sube por el
   carril izquierdo de la avenida y termina sobre el edificio de techo blanco con paneles solares.
2. **Pin de ubicación** del mismo verde que aparece en el fotograma exacto en que la línea llega
   (efecto *pop* con rebote) y queda anclado al edificio aunque la cámara del dron se mueva.

Todo se controla desde un solo archivo (`route.json`); el movimiento de cámara se compensa con
`tracking.json`, generado automáticamente por `track.py`.

```
ruta-overlay/
├── route.json      ← ruta, colores, grosores, tiempos y puntos de rastreo (edita esto)
├── tracking.json   ← homografía por fotograma (ya generado para video_mapa.mov)
├── track.py        ← rastrea puntos fijos del terreno y genera tracking.json
├── render.py       ← hornea la animación en un MP4 / ProRes con alfa / secuencia PNG
├── overlay.html    ← previsualización en el navegador (video + SVG), con scrubber fotograma a fotograma
└── overlay.js      ← lógica de la animación para la web (misma geometría que render.py)
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

El MP4 conserva el audio original. `--scale 0.5` tarda ~2 min; `--scale 1` unos 8–10 min.

### Otras salidas

```bash
# Solo el overlay con canal alfa (ProRes 4444) para montarlo en Premiere, After Effects o DaVinci
python3 render.py --video video_mapa.mov --alpha overlay_alpha.mov --scale 1

# Secuencia PNG con alfa (overlay_0000.png, overlay_0001.png, ...)
python3 render.py --video video_mapa.mov --png-seq overlay_png --scale 1

# Un solo fotograma para revisar (t en segundos)
python3 render.py --video video_mapa.mov --frame 2.7 --out revision_2_7.png
```

En el editor de video, coloca el `.mov` con alfa (o la secuencia PNG) en una pista encima del video
original, alineado al fotograma 0, con el mismo tamaño. No hace falta nada más: el overlay ya trae
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

## Ajustar la animación (`route.json`)

| Clave | Qué controla |
| --- | --- |
| `route.points` | Puntos de control de la ruta, en píxeles del **fotograma 0** a resolución nativa. El primero está fuera de pantalla (y > 3840) para que la línea "entre" por el borde inferior; el último es el edificio (punta del pin). Se suaviza con una spline Catmull-Rom, así que bastan 6–10 puntos. |
| `line.start` / `line.end` | Segundos en que la línea empieza y termina de dibujarse (0.25 → 2.70). El pin aparece exactamente en `line.end`. |
| `line.easing` | `easeInOutCubic` (suave), `easeOutCubic` o `linear`. |
| `line.width` / `line.casing_width` | Grosor del trazo y del borde oscuro (px nativos). |
| `line.shadow_*` | Sombra proyectada de la línea. |
| `pin.animation` | `pop` (escala con rebote desde la punta) o `drop` (cae desde arriba y rebota). |
| `pin.duration`, `pin.overshoot`, `pin.start_scale`, `pin.drop_height` | Ajuste fino del rebote. |
| `pin.height`, `pin.head_radius`, `pin.hole_radius`, `pin.casing_width` | Geometría del pin (px nativos). |
| `pin.ripple` | `true` añade un anillo que se expande al aterrizar (solo en la versión web). |
| `colors.*` | Verde principal `#1FE36A`, borde `#0E8A42`, hueco del pin `#FFFFFF`, sombra `#000000`. |
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
