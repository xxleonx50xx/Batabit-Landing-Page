# Búsqueda en Google · motion graphics sobre fondo chroma

Animación limpia y moderna de una búsqueda en Google, pensada para incrustarse en video con chroma key:
todo ocurre sobre un fondo sólido **azul puro #0000FF** (ningún elemento usa ese tono) a **60 fps** y con
**motion blur real** en los movimientos de escala y desplazamiento.

Secuencia (4.97 s con el texto por defecto):

| Instante | Qué pasa |
| --- | --- |
| 0.00 → 0.55 s | La barra de búsqueda (blanca, bordes redondeados, lupa a la izquierda, micrófono a la derecha) aparece en el centro con un scale-up suave (72 % → 100 %, ligero sobreimpulso) y fundido de entrada. |
| 0.55 → 1.00 s | Reposo con el cursor parpadeando. |
| 1.00 → 2.85 s | Se escribe "Donde está GreenMex?" letra a letra con ritmo humano (85 ms ± 30 ms, pausa extra tras cada espacio); el cursor queda fijo mientras se teclea. |
| 2.85 → 3.50 s | Pausa; el cursor parpadea y desaparece 0.12 s antes del "Enter". |
| 3.50 → 3.82 s | "Enter": la lupa rebota (×1.32) y se tiñe de azul Google #4285F4; la barra da un empujón de 10 px hacia arriba. |
| 3.82 → 4.17 s | Pausa breve. |
| 4.17 → 4.62 s | La barra se desliza 260 px hacia arriba y se desvanece (con motion blur). |
| 4.62 → 4.97 s | Solo el fondo azul. |

```
google-search-chroma/
├── search.json       ← texto, tamaños, colores, tiempos, motion blur (edita esto)
├── search_anim.py    ← renderizador (Python + Pillow + numpy + ffmpeg)
├── fonts/            ← Roboto Regular y Medium (licencia OFL)
└── README.md
```

## Requisitos

- Python 3.9+ con `pillow` y `numpy`: `pip install pillow numpy`
- `ffmpeg` en el PATH (https://ffmpeg.org)

## Uso

```bash
cd google-search-chroma

# Vertical 9:16 (1080x1920) a 60 fps, H.264
python3 search_anim.py --out busqueda_chroma_1080x1920.mp4

# Horizontal 16:9 (1920x1080)
python3 search_anim.py --size 1920x1080 --out busqueda_chroma_1920x1080.mp4

# ProRes 422 HQ con el fondo chroma (sin submuestreo de color 4:2:0: bordes más limpios al hacer el key)
python3 search_anim.py --prores busqueda_chroma_422hq.mov

# Fondo transparente (ProRes 4444 con alfa): se monta directamente, sin chroma key
python3 search_anim.py --alpha busqueda_alpha.mov

# Otro texto, otro chroma, un solo fotograma de revisión, hoja de contactos
python3 search_anim.py --text "Donde está GreenMex?" --bg "#00FF00" --out verde.mp4
python3 search_anim.py --frame 2.4 --out revision.png
python3 search_anim.py --contact hoja.png
```

Un render de 5 s tarda ~2–3 min por salida (los fotogramas con movimiento se componen con 8 sub-fotogramas).

## Chroma key en postproducción

- Color de fondo exacto: `#0000FF` (R 0, G 0, B 255). El script comprueba al arrancar que ningún píxel de los
  elementos coincida con ese color.
- La única zona cercana al tono del fondo es la **sombra** bajo la barra (azul más oscuro). Si el keyer se la
  come o deja halo, usa `--no-shadow` o baja `bar.shadow.opacity` en search.json.
- Para el mejor resultado usa el ProRes 422 HQ (`--prores`) o, mejor aún, el ProRes 4444 con alfa
  (`--alpha`), que no necesita key.
- El MP4 H.264 (4:2:0, CRF 14) es para previsualizar y compartir; también se puede keyear, con un poco de
  supresión de spill en los bordes blancos.

## Ajustes (`search.json`)

Las medidas están en unidades de diseño de un lienzo de 1080 px de ancho (se escalan solas a cualquier tamaño).

| Clave | Qué controla |
| --- | --- |
| `text`, `font` | Texto que se escribe y tipografía (Roboto Regular por defecto; también hay Roboto Medium en `fonts/`). |
| `size`, `fps`, `background` | Tamaño por defecto, framerate y color chroma. Se pueden sobrescribir con `--size`, `--fps`, `--bg`. |
| `bar.*` | Ancho 880, alto 124, radio 62, color blanco, sombra (opacidad 0.22, desenfoque 22, desplazamiento 0/6). `center_y_ratio` sitúa la barra verticalmente. |
| `icons.*` | Tamaño 44, relleno lateral 40, color de la lupa (#9AA0A6) y su color al pulsar Enter (#4285F4); `mic_style` = `google` (azul, verde, amarillo, rojo) o `gray`. |
| `typography.*`, `caret.*` | Tamaño 44 y color #202124 del texto; cursor de 3 × 52 px que parpadea cada 1.06 s. |
| `timing.appear`, `appear_scale_from`, `appear_overshoot` | Duración y curva del scale-up inicial. |
| `timing.type_interval`, `type_jitter`, `type_space_pause`, `type_seed` | Ritmo de escritura (cambia `type_seed` para otra variación "humana"). |
| `timing.pause_after_typing`, `caret_hide_before_enter` | Pausa tras escribir y cuándo desaparece el cursor. |
| `timing.enter`, `enter_icon_scale`, `enter_nudge` | Reacción al Enter: duración, rebote de la lupa y empujón de la barra. |
| `timing.exit`, `exit_style`, `exit_distance`, `tail` | Salida: `slide-fade` (por defecto), `slide` o `fade`; distancia y cola de fondo sólido. |
| `motion_blur.samples`, `shutter` | Sub-fotogramas por fotograma en movimiento (8) y obturador (0.5 = 180°). |
| `supersample` | Antialiasing espacial (2 = render a doble resolución). |
