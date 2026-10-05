"""Gera os icones PNG do app (o celular nao aceita SVG como icone de instalacao).
Desenha o mesmo simbolo de static/icon.svg, sem depender de nenhuma biblioteca.
Rode com: python gerar_icones.py
"""
import struct
import zlib
from pathlib import Path

FUNDO = (0x12, 0x0F, 0x1C)
ANEL = (0xFF, 0x5C, 0x8A)
MIOLO = (0x4D, 0xF0, 0xB5)
AMOSTRAS = 4  # 4x4 por pixel: bordas suaves


def desenhar(tamanho, cantos):
    """cantos=True: quadrado arredondado sobre transparente (icone normal).
    cantos=False: quadrado cheio (iOS e icone 'maskable' recebem a mascara do sistema)."""
    k = tamanho / 512
    centro = 256 * k
    r_ext, r_int, r_miolo = (124 + 22) * k, (124 - 22) * k, 34 * k
    raio_canto = 120 * k
    linhas = []
    for y in range(tamanho):
        linha = bytearray([0])
        for x in range(tamanho):
            acc = [0, 0, 0, 0]
            for sy in range(AMOSTRAS):
                for sx in range(AMOSTRAS):
                    px, py = x + (sx + 0.5) / AMOSTRAS, y + (sy + 0.5) / AMOSTRAS
                    if cantos and not _dentro_quadrado(px, py, tamanho, raio_canto):
                        continue
                    d2 = (px - centro) ** 2 + (py - centro) ** 2
                    if d2 <= r_miolo ** 2:
                        cor = MIOLO
                    elif r_int ** 2 <= d2 <= r_ext ** 2:
                        cor = ANEL
                    else:
                        cor = FUNDO
                    acc[0] += cor[0]; acc[1] += cor[1]; acc[2] += cor[2]; acc[3] += 255
            n = AMOSTRAS * AMOSTRAS
            cobertura = acc[3] // 255
            if cobertura:
                linha += bytes([acc[0] // cobertura, acc[1] // cobertura, acc[2] // cobertura,
                                acc[3] // n])
            else:
                linha += bytes(4)
        linhas.append(bytes(linha))
    return b"".join(linhas)


def _dentro_quadrado(x, y, lado, raio):
    cx = min(max(x, raio), lado - raio)
    cy = min(max(y, raio), lado - raio)
    return (x - cx) ** 2 + (y - cy) ** 2 <= raio ** 2


def gravar_png(caminho, tamanho, pixels):
    def bloco(tipo, dados):
        corpo = tipo + dados
        return struct.pack(">I", len(dados)) + corpo + struct.pack(">I", zlib.crc32(corpo) & 0xFFFFFFFF)

    cabecalho = struct.pack(">IIBBBBB", tamanho, tamanho, 8, 6, 0, 0, 0)
    Path(caminho).write_bytes(
        b"\x89PNG\r\n\x1a\n" + bloco(b"IHDR", cabecalho) + bloco(b"IDAT", zlib.compress(pixels, 9))
        + bloco(b"IEND", b""))


if __name__ == "__main__":
    pasta = Path(__file__).parent / "static"
    for nome, tamanho, cantos in [("icon-192.png", 192, True), ("icon-512.png", 512, True),
                                  ("icon-maskable-512.png", 512, False),
                                  ("apple-touch-icon.png", 180, False)]:
        gravar_png(pasta / nome, tamanho, desenhar(tamanho, cantos))
        print("gerado", nome)
