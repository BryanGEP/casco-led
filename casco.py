#!/usr/bin/env python3
"""
casco.py — Control del casco LED "Magic Display" (DSD-xxxxxx, panel 12x48) por Bluetooth LE.

Protocolo obtenido de la app Android Magic Display 1.4.6 (cn.com.tiro.magicdisplay):
  - Servicio 0000fee9-...  (Quintic QPP)
  - 9600 -> comandos         (Write)
  - 960A -> bloques de datos (Write / Write Without Response)
  - 9601 <- respuestas       (Notify)
  - Todo paquete es de 16 bytes cifrado con AES-128 ECB (llave fija de la app).
  - Pantalla monocromática de 12 filas x 48 columnas (1 bit por LED; el color lo pone el hardware).

Uso rápido:
  python casco.py scan
  python casco.py info
  python casco.py imagen  mi_dibujo.png          [--modo fijo|izq|der|arriba|abajo|parpadeo]
  python casco.py tira    tira_larga.png          (imagen más ancha que 48 px -> se desplaza)
  python casco.py gif     animacion.gif  [--fps 3] [--vueltas 0]   (manda cuadro por cuadro)
  python casco.py brillo  80
  python casco.py velocidad 50
  python casco.py prueba                          (patrón para verificar orientación)
  python casco.py vista   mi_dibujo.png           (previsualiza en la terminal, sin casco)

Requisitos:  pip install bleak pycryptodome pillow
"""
import argparse
import asyncio
import sys
import time

from Crypto.Cipher import AES
from PIL import Image, ImageSequence

# ---------------------------------------------------------------- constantes
KEY = bytes.fromhex("34522a5b7a6e492c08090a9d8d2a23f8")  # libAES.so -> símbolo `key`
UUID_CMD = "d44bc439-abfd-45a2-b575-925416129600"
UUID_DATA = "d44bc439-abfd-45a2-b575-92541612960a"
UUID_NOTIFY = "d44bc439-abfd-45a2-b575-925416129601"
UUID_LIVE = "d44bc439-abfd-45a2-b575-92541612960b"  # dibujo en tiempo real
NOMBRE_PREFIJO = "DSD-"
FILAS, COLUMNAS = 12, 48

MODOS = {"fijo": 1, "parpadeo": 2, "izq": 3, "der": 4, "arriba": 5, "abajo": 6}

_aes = AES.new(KEY, AES.MODE_ECB)


def cifrar(p: bytes) -> bytes:
    return _aes.encrypt(p.ljust(16, b"\0")[:16])


def descifrar(c: bytes) -> bytes:
    return _aes.decrypt(c[:16])


def comando(texto: str, *args: int) -> bytes:
    """Comando estilo app: [longitud, 'T','E','X','T', args..., 0-padding] (16 bytes)."""
    cuerpo = texto.encode() + bytes(a & 0xFF for a in args)
    return bytes([len(cuerpo)]) + cuerpo


# ---------------------------------------------------------------- empaquetado
def empaquetar(pixeles) -> bytes:
    """
    pixeles: lista de columnas; cada columna = 12 valores 0/1 (fila 0 = arriba).
    Cada par de columnas -> 3 bytes (igual que ImageAgreement.getBitmapData1248):
      b0 = filas 0-7 de la col. par   (bit7 = fila 0)
      b1 = filas 8-11 col. par en el nibble alto | filas 8-11 col. impar en el nibble bajo
      b2 = filas 0-7 de la col. impar
    """
    cols = list(pixeles)
    if len(cols) % 2:
        cols.append([0] * FILAS)
    out = bytearray()
    for i in range(0, len(cols), 2):
        a, b = cols[i], cols[i + 1]
        b0 = sum(1 << (7 - r) for r in range(8) if a[r])
        b2 = sum(1 << (7 - r) for r in range(8) if b[r])
        b1 = sum(1 << (7 - (r - 8)) for r in range(8, 12) if a[r]) | \
             sum(1 << (3 - (r - 8)) for r in range(8, 12) if b[r])
        out += bytes([b0, b1, b2])
    return bytes(out)


def imagen_a_columnas(img: Image.Image, umbral: int = 128, invertir: bool = False, ajustar: bool = True):
    """Convierte una imagen a columnas de 12 px. Si no mide 12 de alto, la escala (vecino más cercano)."""
    img = img.convert("RGBA")
    fondo = Image.new("RGBA", img.size, (0, 0, 0, 255))
    img = Image.alpha_composite(fondo, img).convert("RGB")
    if ajustar and img.height != FILAS:
        ancho = max(1, round(img.width * FILAS / img.height))
        img = img.resize((ancho, FILAS), Image.NEAREST)
    px = img.load()
    cols = []
    for x in range(img.width):
        col = []
        for y in range(FILAS):
            r, g, b = px[x, y] if y < img.height else (0, 0, 0)
            on = max(r, g, b) > umbral  # mismo criterio que la app
            col.append(int(on != invertir))
        cols.append(col)
    return cols


def centrar(cols, ancho=COLUMNAS):
    """Rellena con columnas vacías para que quede centrado en 48 de ancho."""
    if len(cols) >= ancho:
        return cols[:ancho]
    izq = (ancho - len(cols)) // 2
    vacia = [0] * FILAS
    return [vacia] * izq + cols + [vacia] * (ancho - len(cols) - izq)


def vista_previa(cols) -> str:
    return "\n".join("".join("█" if c[y] else "·" for c in cols) for y in range(FILAS))


# ---------------------------------------------------------------- BLE
class Casco:
    def __init__(self, direccion=None, retardo=0.06, verbose=False):
        self.direccion = direccion
        self.retardo = retardo  # la app espera 50-60 ms entre bloques
        self.verbose = verbose
        self.espera_vivo = 1.5
        self.cliente = None
        self._cola = asyncio.Queue()

    async def __aenter__(self):
        from bleak import BleakClient, BleakScanner
        if not self.direccion:
            print("Buscando el casco…")
            dev = await BleakScanner.find_device_by_filter(
                lambda d, ad: (d.name or ad.local_name or "").startswith(NOMBRE_PREFIJO), timeout=15)
            if not dev:
                sys.exit("No encontré el casco. ¿Está prendido y Magic Display cerrada?")
            self.direccion = dev.address
            print(f"Encontrado: {dev.name} ({dev.address})")
        self.cliente = BleakClient(self.direccion)
        await self.cliente.connect()
        await self.cliente.start_notify(UUID_NOTIFY, self._al_notificar)
        return self

    async def __aexit__(self, *exc):
        if self.cliente and self.cliente.is_connected:
            await self.cliente.disconnect()

    def _al_notificar(self, _h, data: bytearray):
        p = descifrar(bytes(data))
        n = p[0] if p[0] <= 15 else 15
        txt = p[1:1 + n]
        if self.verbose:
            print("  <-", txt)
        self._cola.put_nowait(txt)

    async def _esperar(self, prefijo: bytes, timeout=3.0):
        fin = time.monotonic() + timeout
        while time.monotonic() < fin:
            try:
                txt = await asyncio.wait_for(self._cola.get(), fin - time.monotonic())
            except asyncio.TimeoutError:
                break
            if txt.startswith(prefijo):
                return txt
        raise TimeoutError(f"El casco no respondió {prefijo!r}")

    async def cmd(self, paquete: bytes):
        if self.verbose:
            print("  ->", paquete)
        await self.cliente.write_gatt_char(UUID_CMD, cifrar(paquete), response=True)

    async def tipo(self) -> str:
        while not self._cola.empty():
            self._cola.get_nowait()
        await self.cmd(comando("STYPE"))
        return (await self._esperar(b"STYPE")).decode(errors="replace")

    async def enviar_datos(self, datos: bytes, intentos: int = 3):
        """DATS(len) -> espera DATSOK -> bloques de 15 bytes -> DATCP -> espera DATCPOK."""
        for intento in range(intentos):
            while not self._cola.empty():
                self._cola.get_nowait()
            await self.cmd(comando("DATS", len(datos) >> 8, len(datos) & 0xFF, 0, 0))  # = {8,'DATS',hi,lo,0,0}
            try:
                await self._esperar(b"DATSOK", timeout=2)
                break
            except TimeoutError:
                if intento == intentos - 1:
                    raise
                await asyncio.sleep(0.3)
        for i in range(0, len(datos), 15):
            trozo = datos[i:i + 15]
            await self.cliente.write_gatt_char(UUID_DATA, cifrar(bytes([len(trozo)]) + trozo), response=False)
            await asyncio.sleep(self.retardo)
        await self.cmd(comando("DATCP"))
        await self._esperar(b"DATCPOK", timeout=5)

    async def mostrar(self, cols, modo: int = 1):
        await self.enviar_datos(empaquetar(cols))
        await self.cmd(comando("MODE", modo))
        await asyncio.sleep(0.15)  # deja que el casco termine de guardar antes del siguiente envío

    # ---- modo en vivo (el que usa la app al dibujar con el dedo) ----
    # SMVEW 1 entra al modo; luego cada columna va en su propio paquete por 960B:
    #   [3, columna, alto, bajo]  con  palabra16 = suma(1 << (15 - fila))  (verificado en el casco)
    async def vivo_entrar(self):
        await self.cmd(comando("SMVEW", 1))
        await asyncio.sleep(self.espera_vivo)  # el casco borra la pantalla ~0.5 s después de entrar
        self._ultimo = None

    async def vivo_salir(self, guardar=False):
        await self.cmd(comando("SMVEW", 2 if guardar else 0))

    async def vivo_cuadro(self, cols, pausa=0.02, confirmar=True, completo=False):
        """Manda solo las columnas que cambiaron respecto al cuadro anterior (o todas si completo=True)."""
        previo = None if completo else getattr(self, "_ultimo", None)
        for x, col in enumerate(cols[:COLUMNAS]):
            if previo is not None and previo[x] == col:
                continue
            palabra = sum(1 << (15 - r) for r in range(FILAS) if col[r])  # fila 0 (arriba) = bit 15
            paquete = bytes([3, x, palabra >> 8, palabra & 0xFF])
            # response=True: espera a que el casco confirme cada columna (si no, se pierden paquetes)
            await self.cliente.write_gatt_char(UUID_LIVE, cifrar(paquete), response=confirmar)
            if pausa:
                await asyncio.sleep(pausa)
        self._ultimo = [list(c) for c in cols]

    async def brillo(self, v: int):
        await self.cmd(comando("LIGHT", max(1, min(100, v))))

    async def velocidad(self, v: int):
        await self.cmd(comando("SPEED", max(0, min(100, v))))


# ---------------------------------------------------------------- CLI
def cargar_columnas(ruta, args, para_tira=False):
    cols = imagen_a_columnas(Image.open(ruta), args.umbral, args.invertir)
    return cols if para_tira else centrar(cols)


async def main():
    ap = argparse.ArgumentParser(description="Control del casco LED Magic Display (12x48)")
    ap.add_argument("accion", choices=["scan", "info", "imagen", "tira", "gif", "brillo", "velocidad", "prueba", "vista"])
    ap.add_argument("valor", nargs="?")
    ap.add_argument("--modo", default="fijo", choices=list(MODOS))
    ap.add_argument("--umbral", type=int, default=128, help="0-255: qué tan claro debe ser un pixel para encenderse")
    ap.add_argument("--invertir", action="store_true", help="enciende lo oscuro en vez de lo claro")
    ap.add_argument("--fps", type=float, default=3.0)
    ap.add_argument("--vueltas", type=int, default=0, help="repeticiones del GIF (0 = infinito)")
    ap.add_argument("--direccion", help="dirección BLE del casco (opcional)")
    ap.add_argument("--retardo", type=float, default=0.06, help="segundos entre bloques BLE")
    ap.add_argument("--vivo", action="store_true", help="usa el modo de dibujo en tiempo real (más rápido para GIFs)")
    ap.add_argument("--pausa", type=float, default=0.02, help="segundos entre columnas en modo en vivo")
    ap.add_argument("--sin-confirmar", action="store_true", help="modo en vivo sin esperar confirmación (más rápido, puede perder columnas)")
    ap.add_argument("--espera", type=float, default=1.5, help="segundos a esperar tras entrar al modo en vivo")
    ap.add_argument("--completo", action="store_true", help="modo en vivo: manda las 48 columnas en cada cuadro (evita pixeles 'fantasma')")
    ap.add_argument("-v", "--verbose", action="store_true")
    a = ap.parse_args()

    if a.accion == "vista":
        cols = cargar_columnas(a.valor, a, para_tira=True)
        print(vista_previa(cols if len(cols) > COLUMNAS else centrar(cols)))
        print(f"\n{len(cols)} columnas x {FILAS} filas -> {len(empaquetar(cols))} bytes")
        return

    if a.accion == "scan":
        from bleak import BleakScanner
        for d in await BleakScanner.discover(timeout=8):
            if (d.name or "").startswith(NOMBRE_PREFIJO):
                print(d.address, d.name)
        return

    async with Casco(a.direccion, a.retardo, a.verbose) as casco:
        casco.espera_vivo = a.espera
        if a.accion == "info":
            print("Tipo de pantalla:", await casco.tipo())

        elif a.accion == "brillo":
            await casco.brillo(int(a.valor))

        elif a.accion == "velocidad":
            await casco.velocidad(int(a.valor))

        elif a.accion == "prueba":
            # Borde + una "F" pegada a la izquierda: si la F se ve al revés o volteada, sabemos cómo rotar.
            cols = [[0] * FILAS for _ in range(COLUMNAS)]
            for x in range(COLUMNAS):
                cols[x][0] = cols[x][FILAS - 1] = 1
            for y in range(FILAS):
                cols[0][y] = cols[COLUMNAS - 1][y] = 1
            for y in range(2, 10):
                cols[3][y] = 1          # palo de la F
            for x in range(3, 9):
                cols[x][2] = 1          # brazo de arriba
            for x in range(3, 7):
                cols[x][5] = 1          # brazo de en medio
            print(vista_previa(cols))
            if a.vivo:
                await casco.vivo_entrar()
                await casco.vivo_cuadro(cols, a.pausa, not a.sin_confirmar)
                input("Mira el casco y presiona Enter para salir del modo en vivo…")
                await casco.vivo_salir()
            else:
                await casco.mostrar(cols, 1)

        elif a.accion == "imagen":
            cols = cargar_columnas(a.valor, a)
            print(vista_previa(cols))
            await casco.mostrar(cols, MODOS[a.modo])

        elif a.accion == "tira":
            cols = cargar_columnas(a.valor, a, para_tira=True)
            modo = MODOS[a.modo] if a.modo != "fijo" else MODOS["izq"]
            print(f"{len(cols)} columnas, modo {modo}")
            await casco.mostrar(cols, modo)

        elif a.accion == "gif":
            gif = Image.open(a.valor)
            cuadros = [centrar(imagen_a_columnas(f.copy(), a.umbral, a.invertir)) for f in ImageSequence.Iterator(gif)]
            print(f"{len(cuadros)} cuadros, método {'en vivo' if a.vivo else 'carga'}. Ctrl+C para parar.")
            if a.vivo:
                await casco.vivo_entrar()
            vuelta = 0
            try:
                while a.vueltas == 0 or vuelta < a.vueltas:
                    for i, c in enumerate(cuadros):
                        t0 = time.monotonic()
                        try:
                            if a.vivo:
                                await casco.vivo_cuadro(c, a.pausa, not a.sin_confirmar, a.completo)
                            else:
                                await casco.mostrar(c, 1)
                        except TimeoutError as e:
                            print(f"  cuadro {i}: {e}")
                        dt = time.monotonic() - t0
                        if a.verbose:
                            print(f"  cuadro {i}: {dt*1000:.0f} ms")
                        await asyncio.sleep(max(0, 1 / a.fps - dt))
                    vuelta += 1
            finally:
                if a.vivo and casco.cliente.is_connected:
                    await casco.vivo_salir()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
