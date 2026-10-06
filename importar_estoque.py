"""Lanca uma compra de estoque a partir de um JSON (produtos, EAN/SKU, quantidades, custos e precos).

Regras:
  - nao duplica produto: casa por EAN, depois SKU, depois um apelido fixo, depois pelo nome;
  - produto que ja existe so recebe a quantidade (soma ao estoque) e uma "entrada de estoque";
  - o custo do produto vira a media ponderada entre o estoque antigo e o novo (se o antigo tinha custo);
  - o preco de venda do arquivo passa a valer;
  - rodar o mesmo arquivo duas vezes nao soma duas vezes (cada entrada guarda a referencia da compra).

Uso:
  python importar_estoque.py arquivo.json --dry-run     # mostra o que faria, sem gravar
  python importar_estoque.py arquivo.json               # grava
"""
import argparse
import hashlib
import json
import re
import sys
import unicodedata

from db import get_connection, init_db
from servicos import brl, hoje, qtd_texto

# produtos que o app ja tinha com outro nome (numero do item no cardapio)
APELIDOS = {"COCA-ORIG-350": 9, "COCA-PET-200": 8, "GUARANA-ANT-PET-200": 13}


def chave(texto):
    t = unicodedata.normalize("NFD", str(texto or ""))
    t = "".join(c for c in t if unicodedata.category(c) != "Mn")
    return re.sub(r"[^A-Z0-9]+", " ", t.upper()).strip()


def localizar(cur, p):
    if p.get("ean"):
        cur.execute("SELECT * FROM produtos WHERE ean = %s", (p["ean"],))
        row = cur.fetchone()
        if row:
            return row, "EAN"
    if p.get("sku"):
        cur.execute("SELECT * FROM produtos WHERE sku = %s", (p["sku"],))
        row = cur.fetchone()
        if row:
            return row, "SKU"
        if p["sku"] in APELIDOS:
            cur.execute("SELECT * FROM produtos WHERE numero_item = %s", (APELIDOS[p["sku"]],))
            row = cur.fetchone()
            if row:
                return row, "apelido"
    cur.execute("SELECT * FROM produtos WHERE ativo")
    alvo = chave(p["nome"])
    for row in cur.fetchall():
        if chave(row["nome"]) == alvo:
            return row, "nome"
    return None, None


def importar(caminho, dry_run=False):
    bruto = open(caminho, "rb").read()
    dados = json.loads(bruto.decode("utf-8"))
    referencia = "compra-" + hashlib.sha1(bruto).hexdigest()[:10]
    produtos = dados["produtos"]

    init_db()
    conn = get_connection()
    cur = conn.cursor()
    dia = hoje().isoformat()

    novos, somados, pulados, precos = [], [], [], []
    unidades = 0
    gasto = 0.0
    for p in produtos:
        qtd = float(p["quantidade_estoque"])
        custo_u = float(p["custo_unitario"])
        custo_t = float(p["custo_total"])
        preco = float(p["preco_venda"])
        estimado = bool(p.get("quantidade_estimada"))
        obs = p.get("observacao_quantidade") or None

        prod, como = localizar(cur, p)
        if prod is None:
            cur.execute(
                """INSERT INTO produtos (numero_item, nome, preco, custo, categoria, estoque, ean, sku, grupo)
                   VALUES (NULL, %s, %s, %s, 'mercadinho', 0, %s, %s, %s) RETURNING *""",
                (p["nome"], preco, custo_u, p.get("ean"), p.get("sku"), p.get("categoria")),
            )
            prod = cur.fetchone()
            novos.append(p["nome"])
            estoque_antes, custo_antes = 0.0, 0.0
        else:
            cur.execute("SELECT 1 FROM entradas_estoque WHERE referencia = %s AND produto_id = %s",
                        (referencia, prod["id"]))
            if cur.fetchone():
                pulados.append(prod["nome"])
                continue
            estoque_antes, custo_antes = float(prod["estoque"]), float(prod["custo"] or 0)
            somados.append(f"{prod['nome']} (achado por {como}): {qtd_texto(estoque_antes)} + {qtd_texto(qtd)}")
            if abs(prod["preco"] - preco) > 0.001:
                precos.append(f"{prod['nome']}: {brl(prod['preco'])} -> {brl(preco)}")

        if estoque_antes > 0 and custo_antes > 0:
            custo_novo = round((estoque_antes * custo_antes + qtd * custo_u) / (estoque_antes + qtd), 4)
        else:
            custo_novo = custo_u
        cur.execute(
            """UPDATE produtos SET estoque = estoque + %s, custo = %s, preco = %s,
                      ean = COALESCE(ean, %s), sku = COALESCE(sku, %s), grupo = COALESCE(grupo, %s),
                      estoque_estimado = estoque_estimado OR %s,
                      obs_estoque = CASE WHEN %s THEN %s ELSE obs_estoque END
               WHERE id = %s""",
            (qtd, custo_novo, preco, p.get("ean"), p.get("sku"), p.get("categoria"),
             estimado, estimado, p.get("observacao_quantidade"), prod["id"]),
        )
        cur.execute(
            """INSERT INTO entradas_estoque (data, produto_id, quantidade, observacoes, custo_unitario,
                                             custo_total, preco_venda, referencia)
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s)""",
            (dia, prod["id"], qtd, ("Compra de estoque. " + obs) if obs else "Compra de estoque",
             custo_u, custo_t, preco, referencia),
        )
        unidades += qtd
        gasto += custo_t

    if (novos or somados) and not dry_run:
        cur.execute("INSERT INTO historico (quem, acao, detalhe) VALUES (%s, %s, %s)",
                    ("Importação", "Compra de estoque",
                     f"{int(unidades)} unidades, custo {brl(gasto)} ({len(novos)} produtos novos, "
                     f"{len(somados)} existentes)"))
    resumo = dados.get("resumo_dashboard", {})
    if dry_run:
        conn.rollback()
    else:
        conn.commit()
    conn.close()

    print(("[SIMULACAO, nada foi gravado] " if dry_run else "") + f"Compra {referencia} em {dia}")
    print(f"  Unidades lancadas: {qtd_texto(unidades)} (arquivo diz {resumo.get('quantidade_total_unidades_vendaveis')})")
    print(f"  Custo lancado: {brl(gasto)} (arquivo diz {brl(resumo.get('gasto_total_mercadorias_consideradas'))})")
    print(f"  Produtos novos ({len(novos)}): " + "; ".join(novos))
    print(f"  Produtos que ja existiam ({len(somados)}):")
    for s in somados:
        print("    ", s)
    print(f"  Precos alterados ({len(precos)}):")
    for s in precos:
        print("    ", s)
    if pulados:
        print(f"  Ja lancados antes e pulados ({len(pulados)}): " + "; ".join(pulados))


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("arquivo")
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()
    try:
        importar(a.arquivo, dry_run=a.dry_run)
    except FileNotFoundError:
        sys.exit(f"Arquivo nao encontrado: {a.arquivo}")
