"""Gera um .xlsx de backup/relatorio a partir do banco SQLite atual,
no mesmo formato da planilha original (uma aba de lancamentos + uma de cardapio).
"""
import io
from datetime import datetime
from pathlib import Path

import openpyxl

from db import get_connection

CABECALHO_PEDIDOS = [
    "DATA", "PESSOA", "CATEGORIA", "N. ITEM", "PRODUTO", "QUANTIDADE", "VALOR UNITARIO",
    "VALOR TOTAL", "CUSTO UNITARIO", "CUSTO TOTAL", "LUCRO", "FORMA DE PAGAMENTO", "PAGO?",
    "DATA DO PAGAMENTO", "OBSERVACOES", "VALOR PAGO",
]
CABECALHO_CARDAPIO = ["N. Item", "Produto", "Categoria", "Preco", "Custo", "Estoque atual"]
CABECALHO_ESTOQUE = ["Data", "Categoria", "Produto", "Quantidade", "Observacoes"]
CABECALHO_PAGAMENTOS = ["Data", "Pessoa", "Valor", "Forma de pagamento", "Registrado por"]


def _montar():
    conn = get_connection()
    wb = openpyxl.Workbook()

    ws = wb.active
    ws.title = "LANCAMENTOS"
    ws.append(CABECALHO_PEDIDOS)

    cur = conn.cursor()
    cur.execute(
        """SELECT p.data, pe.nome AS pessoa, pr.categoria, pr.numero_item, pr.nome AS produto,
                  p.quantidade, p.valor_unitario, p.valor_total, p.custo_unitario, p.custo_total,
                  p.forma_pagamento, p.pago, p.data_pagamento, p.observacoes, p.valor_pago
           FROM pedidos p
           JOIN pessoas pe ON pe.id = p.pessoa_id
           JOIN produtos pr ON pr.id = p.produto_id
           ORDER BY p.data, p.id"""
    )
    pedidos = cur.fetchall()

    for row in pedidos:
        ws.append([
            row["data"], row["pessoa"], row["categoria"], row["numero_item"], row["produto"],
            row["quantidade"], row["valor_unitario"], row["valor_total"],
            row["custo_unitario"], row["custo_total"], row["valor_total"] - row["custo_total"],
            row["forma_pagamento"], "PAGO" if row["pago"] else "PENDENTE",
            row["data_pagamento"], row["observacoes"], row["valor_pago"],
        ])

    ws2 = wb.create_sheet("CARDAPIO")
    ws2.append(CABECALHO_CARDAPIO)
    cur.execute(
        "SELECT numero_item, nome, categoria, preco, custo, estoque FROM produtos WHERE ativo "
        "ORDER BY categoria, numero_item"
    )
    produtos = cur.fetchall()
    for row in produtos:
        ws2.append([row["numero_item"], row["nome"], row["categoria"], row["preco"], row["custo"],
                    row["estoque"]])

    ws3 = wb.create_sheet("ENTRADAS_ESTOQUE")
    ws3.append(CABECALHO_ESTOQUE)
    cur.execute(
        """SELECT e.data, pr.categoria, pr.nome AS produto, e.quantidade, e.observacoes
           FROM entradas_estoque e JOIN produtos pr ON pr.id = e.produto_id
           ORDER BY e.data, e.id"""
    )
    entradas = cur.fetchall()
    for row in entradas:
        ws3.append([row["data"], row["categoria"], row["produto"], row["quantidade"], row["observacoes"]])

    ws4 = wb.create_sheet("PAGAMENTOS")
    ws4.append(CABECALHO_PAGAMENTOS)
    cur.execute(
        """SELECT g.data, pe.nome AS pessoa, g.valor, g.forma_pagamento, g.criado_por
           FROM pagamentos g JOIN pessoas pe ON pe.id = g.pessoa_id ORDER BY g.data, g.id"""
    )
    for row in cur.fetchall():
        ws4.append([row["data"], row["pessoa"], row["valor"], row["forma_pagamento"], row["criado_por"]])

    conn.close()
    return wb


def exportar_bytes():
    """O Excel de backup em memoria (usado pelo download no app e pelo envio no Telegram)."""
    saida = io.BytesIO()
    _montar().save(saida)
    return saida.getvalue()


def exportar(destino=None):
    """Grava o Excel em disco (uso pela linha de comando)."""
    if destino is None:
        pasta = Path(__file__).parent / "data" / "backups"
        pasta.mkdir(parents=True, exist_ok=True)
        destino = pasta / f"palacios_backup_{datetime.now():%Y%m%d_%H%M%S}.xlsx"
    _montar().save(destino)
    return str(destino)


if __name__ == "__main__":
    caminho = exportar()
    print(f"Backup gerado em: {caminho}")
