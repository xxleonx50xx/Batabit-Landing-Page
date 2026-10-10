# Búsqueda en Google · motion graphics sobre fondo chroma

Animación limpia, moderna y dinámica de una búsqueda en Google, pensada para incrustarse en video con chroma
key: todo ocurre sobre un fondo sólido **azul puro #0000FF** (ningún elemento usa ese tono) a **60 fps** y con
**motion blur real** en todos los movimientos.

Secuencia (5.24 s con el texto por defecto):

| Instante | Qué pasa |
| --- | --- |
| 0.00 → 0.60 s | La barra (blanca, bordes redondeados) entra con un rebote elástico desde el centro (55 % → 100 %, sobreimpulso) y una leve subida; la lupa y el micrófono aparecen en cascada con su propio rebote. |
| 0.60 → 1.00 s | Reposo con el cursor parpadeando. Una flotación de ±3 px mantiene vivo el conjunto durante toda la pieza. |
| 1.00 → 3.00 s | Se escribe "¿Donde está GreenMex?" letra a letra con ritmo humano (85 ms ± 30 ms, pausa extra tras cada espacio). Cada letra "salta" al teclearse y el cursor da un pequeño pulso; queda fijo mientras se teclea y parpadea en las pausas. |
| 1.54 s | A partir de la sexta letra se despliega un panel de sugerencias tipo Google (la primera fila completa lo que falta por escribir en negrita; las demás son sugerencias editables). Las filas entran escalonadas. |
| 3.00 → 3.60 s | Pausa; el cursor desaparece y la primera sugerencia se resalta en gris. |
| 3.60 → 4.00 s | "Enter": la barra se presiona y da un empujón de 12 px, la lupa rebota (×1.4) y se tiñe de azul Google emitiendo una onda tipo Material, y el panel se pliega. |
| 3.72 → 4.27 s | Una línea de progreso azul recorre el borde inferior de la barra ("buscando…"). |
| 4.47 → 4.89 s | La barra sale disparada hacia arriba con estiramiento, fundido y motion blur. |
| 4.89 → 5.24 s | Solo el fondo azul. |

```
google-search-chroma/
├── search.json       ← texto, sugerencias, tamaños, colores, tiempos y motion blur (edita esto)
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
python3 search_anim.py --text "¿Dónde está GreenMex?" --bg "#00FF00" --out verde.mp4
python3 search_anim.py --frame 2.4 --out revision.png
python3 search_anim.py --contact hoja.png
```

Un render de 5 s tarda ~3–5 min por salida (casi todos los fotogramas tienen movimiento y se componen con
4 u 8 sub-fotogramas).

## Chroma key en postproducción

- Color de fondo exacto: `#0000FF` (R 0, G 0, B 255). El script comprueba al arrancar que ningún píxel de los
  elementos coincida con ese color.
- La única zona cercana al tono del fondo es la **sombra** bajo la barra y el panel (azul más oscuro). Si el
  keyer se la come o deja halo, usa `--no-shadow` o baja `bar.shadow.opacity` en search.json.
- Para el mejor resultado usa el ProRes 422 HQ (`--prores`) o, mejor aún, el ProRes 4444 con alfa
  (`--alpha`), que no necesita key.
- El MP4 H.264 (4:2:0, CRF 14) es para previsualizar y compartir; también se puede keyear, con un poco de
  supresión de spill en los bordes blancos.

## Ajustes (`search.json`)

Las medidas están en unidades de diseño de un lienzo de 1080 px de ancho (se escalan solas a cualquier tamaño).

| Clave | Qué controla |
| --- | --- |
| `text`, `font`, `font_medium` | Texto que se escribe y tipografías (Roboto Regular para el texto, Medium para la parte en negrita de la primera sugerencia). |
| `size`, `fps`, `background` | Tamaño por defecto, framerate y color chroma. Se pueden sobrescribir con `--size`, `--fps`, `--bg`. |
| `bar.*` | Ancho 880, alto 124, radio 62, color blanco, sombra (opacidad 0.22, desenfoque 22, desplazamiento 0/6). `center_y_ratio` sitúa la barra verticalmente. |
| `icons.*` | Tamaño 44, relleno lateral 40, color de la lupa (#9AA0A6) y su color al pulsar Enter (#4285F4); `mic_style` = `google` (azul, verde, amarillo, rojo) o `gray`. |
| `typography.*`, `caret.*` | Tamaño 44 y color #202124 del texto; cursor de 3 × 52 px que parpadea cada 1.06 s. |
| `dropdown.*` | Panel de sugerencias: `enabled`, `after_chars` (letra a partir de la cual aparece), `suggestions` (las filas 2–4), alto de fila, tamaño de letra, tiempos de apertura/cierre/escalonado, color del resaltado (`hover_color`) y del acento. |
| `timing.appear`, `appear_scale_from`, `appear_overshoot`, `appear_rise` | Entrada de la barra: duración, escala inicial, rebote y subida. |
| `timing.icons_pop_delay`, `icons_pop` | Cuándo y cuánto tardan en aparecer la lupa y el micrófono. |
| `timing.idle_float`, `idle_float_period` | Amplitud (px) y periodo (s) de la flotación continua; 0 la desactiva. |
| `timing.type_interval`, `type_jitter`, `type_space_pause`, `type_seed` | Ritmo de escritura (cambia `type_seed` para otra variación "humana"). |
| `timing.char_pop`, `char_rise` | Salto de cada letra al aparecer: duración y altura. |
| `timing.pause_after_typing`, `caret_hide_before_enter` | Pausa tras escribir y cuándo desaparece el cursor. |
| `timing.enter`, `enter_icon_scale`, `enter_press`, `enter_nudge`, `ripple` | Reacción al Enter: duración, rebote de la lupa, presión y empujón de la barra, duración de la onda. |
| `timing.progress_delay`, `progress` | Línea de progreso azul: retardo tras el Enter y duración del barrido. |
| `timing.exit`, `exit_style`, `exit_distance`, `exit_stretch`, `tail` | Salida: `slide-fade` (por defecto), `slide` o `fade`; distancia, estiramiento y cola de fondo sólido. |
| `motion_blur.samples`, `samples_small`, `shutter` | Sub-fotogramas por fotograma en movimiento grande (8) y pequeño (4), y obturador (0.5 = 180°). |
| `supersample` | Antialiasing espacial (2 = render a doble resolución). |
