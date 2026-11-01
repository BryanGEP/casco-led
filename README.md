# Casco LED

Control web para un casco con visera LED (pantalla de 12×48) que originalmente solo se maneja con la app *Magic Display*. Permite cargar pixel art, animaciones GIF y texto propio desde el navegador, por Bluetooth.

**Página:** https://bryangep.github.io/casco-led/

## Qué hace

- **Galería** con 20 animaciones y tiras listas para enviar.
- **Texto:** escribes con el teclado y se envía fijo o desplazándose.
- **Editor de pixel art** de 48×12 con cuadros para animar.
- **Importar** PNG, JPG, GIF o WEBP (se convierten a 12 px de alto, blanco y negro).
- Brillo, velocidad de desplazamiento y modos (fijo, desplazar, parpadeo).
- Funciona en **iPhone con Bluefy** y en **PC con Chrome o Edge** (Web Bluetooth).

## Cómo se hizo

El protocolo no está documentado, así que se obtuvo por ingeniería inversa:

1. Con **nRF Connect** se identificaron el servicio BLE `FEE9` y sus características.
2. Se descompiló la app Android con **jadx**: los paquetes van cifrados con **AES-128 ECB** y la llave estaba en una librería nativa (`libAES.so`).
3. Se validó descifrando la respuesta real del casco (`STYPE12X48N`) y se reimplementó el protocolo en Python y en JavaScript.

| Característica | Uso |
|---|---|
| `…9600` | Comandos (`DATS`, `DATCP`, `MODE`, `LIGHT`, `SPEED`, `SMVEW`) |
| `…960A` | Datos de imagen en bloques de 15 bytes |
| `…960B` | Dibujo en tiempo real, una columna por paquete |
| `…9601` | Respuestas del casco (notify) |

## Archivos

- `index.html`: la app web completa en un solo archivo.
- `casco.py`: herramienta de línea de comandos en Python (`bleak`, `pycryptodome`, `pillow`).

```
pip install bleak pycryptodome pillow
python casco.py info
python casco.py gif animacion.gif --vivo --pausa 0 --fps 4
python casco.py tira tira.png --modo izq
```

## Créditos

- Iconos de la galería basados en [pixelarticons](https://github.com/halfmage/pixelarticons) de Gerrit Halfmann (MIT).
- Fuente de texto [Spleen](https://github.com/fcambus/spleen) 6×12 de Frederic Cambus (BSD-2).
