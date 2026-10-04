"""Importa os dados da planilha original (cardapio + lancamentos ja existentes)
para o banco Postgres (Supabase). Rode uma unica vez (ou de novo se quiser
reimportar do zero com --reset).

Precisa da variavel de ambiente DATABASE_URL configurada (veja .env.example).
"""
import argparse
import sys
from datetime import date, datetime

import openpyxl

from db import get_connection, get_or_create_pessoa, init_db


def normaliza_status(valor):
    if not valor:
        return False
    return str(valor).strip().lower().startswith("pago")


def to_iso_date(valor):
    if isinstance(valor, datetime):
        return valor.date().isoformat()
    if isinstance(valor, date):
        return valor.isoformat()
    if isinstance(valor, str) and valor.strip():
        return valor.strip()
    return date.today().isoformat()


def importar(caminho_xlsx, reset=False, min_row_pedidos=2):
    init_db()
    conn = get_connection()
    cur = conn.cursor()

    if reset:
        cur.execute("DELETE FROM pedidos; DELETE FROM produtos; DELETE FROM pessoas;")
        conn.commit()

    wb = openpyxl.load_workbook(caminho_xlsx, data_only=True)
    ws_cardapio = wb.worksheets[1]
    ws_pedidos = wb.worksheets[0]

    # --- cardapio ---
    produtos_importados = 0
    for row in ws_cardapio.iter_rows(min_row=2, values_only=True):
        numero_item, nome, preco = row[0], row[1], row[2]
        if numero_item is None or nome is None:
            continue
        cur.execute("SELECT id FROM produtos WHERE numero_item = %s", (numero_item,))
        existente = cur.fetchone()
        if existente:
            cur.execute(
                "UPDATE produtos SET nome = %s, preco = %s WHERE id = %s",
                (str(nome).strip(), float(preco or 0), existente["id"]),
            )
        else:
            cur.execute(
                "INSERT INTO produtos (numero_item, nome, preco) VALUES (%s, %s, %s)",
                (numero_item, str(nome).strip(), float(preco or 0)),
            )
        produtos_importados += 1
    conn.commit()

    # --- pedidos existentes (ignora linhas em branco / template) ---
    pedidos_importados = 0
    for row in ws_pedidos.iter_rows(min_row=min_row_pedidos, values_only=True):
        (data_val, pessoa, num_item, produto_nome, qtd, valor_unit,
         valor_total, forma_pag, pago, data_pagto, obs) = (list(row) + [None] * 11)[:11]

        if not pessoa or not num_item:
            continue

        pessoa_id = get_or_create_pessoa(conn, str(pessoa))

        cur.execute("SELECT id, preco FROM produtos WHERE numero_item = %s", (num_item,))
        prod_row = cur.fetchone()
        if not prod_row:
            print(f"  aviso: item {num_item!r} nao encontrado no cardapio, pulando linha ({pessoa!r})")
            continue

        vu = float(valor_unit) if isinstance(valor_unit, (int, float)) else prod_row["preco"]
        qtd_f = float(qtd) if isinstance(qtd, (int, float)) else 1.0
        vt = float(valor_total) if isinstance(valor_total, (int, float)) else vu * qtd_f
        pago_bool = normaliza_status(pago)

        cur.execute(
            """INSERT INTO pedidos
               (data, pessoa_id, produto_id, quantidade, valor_unitario, valor_total,
                forma_pagamento, pago, data_pagamento, observacoes)
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)""",
            (
                to_iso_date(data_val),
                pessoa_id,
                prod_row["id"],
                qtd_f,
                vu,
                vt,
                str(forma_pag).strip() if forma_pag else None,
                pago_bool,
                to_iso_date(data_pagto) if pago_bool and data_pagto else None,
                str(obs).strip() if obs else None,
            ),
        )
        pedidos_importados += 1

    conn.commit()
    conn.close()
    print(f"Produtos importados/atualizados: {produtos_importados}")
    print(f"Pedidos importados: {pedidos_importados}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("xlsx", nargs="?",
                         default=r"C:\Users\yan.santana\Downloads\PALACIO´S_com_pedido_automatico.xlsx",
                         help="Caminho da planilha original")
    parser.add_argument("--reset", action="store_true",
                         help="Apaga tudo que ja existe no banco antes de importar")
    parser.add_argument("--min-row-pedidos", type=int, default=2,
                         help="Primeira linha da aba de lancamentos a importar (use para "
                              "importar so as linhas novas de uma planilha atualizada, sem "
                              "duplicar o que ja foi importado antes)")
    args = parser.parse_args()

    try:
        importar(args.xlsx, reset=args.reset, min_row_pedidos=args.min_row_pedidos)
    except FileNotFoundError:
        print(f"Arquivo nao encontrado: {args.xlsx}")
        sys.exit(1)
