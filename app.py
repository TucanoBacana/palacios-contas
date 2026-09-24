from datetime import date

from flask import Flask, flash, redirect, render_template, request, send_file, url_for

from db import get_connection, get_or_create_pessoa, init_db
from export_xlsx import exportar

FORMAS_PAGAMENTO = ["Pix", "Dinheiro", "Cartao", "Transferencia", "Outro"]

app = Flask(__name__)
app.secret_key = "palacios-contas"  # uso interno, sem dados sensiveis

init_db()


@app.route("/")
def dashboard():
    conn = get_connection()
    with conn.cursor() as cur:
        cur.execute(
            """SELECT
                 COALESCE(SUM(valor_total) FILTER (WHERE NOT pago), 0) AS total_pendente,
                 COALESCE(SUM(valor_total) FILTER (WHERE pago), 0) AS total_pago,
                 COUNT(*) FILTER (WHERE NOT pago) AS qtd_pendente
               FROM pedidos"""
        )
        totais = cur.fetchone()

        cur.execute(
            """SELECT pe.nome, SUM(p.valor_total) AS total
               FROM pedidos p JOIN pessoas pe ON pe.id = p.pessoa_id
               WHERE NOT p.pago
               GROUP BY pe.id, pe.nome
               ORDER BY total DESC
               LIMIT 8"""
        )
        ranking = cur.fetchall()

        cur.execute(
            """SELECT p.id, p.data, pe.nome AS pessoa, pr.nome AS produto, p.quantidade,
                      p.valor_total, p.pago
               FROM pedidos p
               JOIN pessoas pe ON pe.id = p.pessoa_id
               JOIN produtos pr ON pr.id = p.produto_id
               ORDER BY p.id DESC
               LIMIT 10"""
        )
        ultimos = cur.fetchall()
    conn.close()

    return render_template(
        "dashboard.html", totais=totais, ranking=ranking, ultimos=ultimos
    )


@app.route("/novo", methods=["GET", "POST"])
def novo():
    conn = get_connection()

    if request.method == "POST":
        pessoa_nome = request.form.get("pessoa", "").strip()
        produto_id = request.form.get("produto_id", type=int)
        quantidade = request.form.get("quantidade", type=float) or 1
        forma_pagamento = request.form.get("forma_pagamento") or "Pix"
        pago = request.form.get("pago") == "on"
        observacoes = request.form.get("observacoes", "").strip() or None
        data_lanc = request.form.get("data") or date.today().isoformat()

        erro = None
        if not pessoa_nome:
            erro = "Informe o nome da pessoa."
        elif not produto_id:
            erro = "Selecione um produto."

        if erro:
            flash(erro, "erro")
        else:
            with conn.cursor() as cur:
                cur.execute("SELECT * FROM produtos WHERE id = %s", (produto_id,))
                produto = cur.fetchone()
                pessoa_id = get_or_create_pessoa(conn, pessoa_nome)
                valor_unitario = produto["preco"]
                valor_total = round(valor_unitario * quantidade, 2)
                data_pagamento = data_lanc if pago else None

                cur.execute(
                    """INSERT INTO pedidos
                       (data, pessoa_id, produto_id, quantidade, valor_unitario, valor_total,
                        forma_pagamento, pago, data_pagamento, observacoes)
                       VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)""",
                    (data_lanc, pessoa_id, produto_id, quantidade, valor_unitario, valor_total,
                     forma_pagamento, pago, data_pagamento, observacoes),
                )
            conn.commit()
            flash(f"Lancamento registrado: {pessoa_nome} - {produto['nome']} (R$ {valor_total:.2f})", "ok")
            conn.close()
            return redirect(url_for("novo"))

    with conn.cursor() as cur:
        cur.execute("SELECT * FROM produtos WHERE ativo ORDER BY nome")
        produtos = cur.fetchall()
        cur.execute("SELECT nome FROM pessoas ORDER BY nome")
        pessoas = cur.fetchall()
    conn.close()

    return render_template(
        "novo.html",
        produtos=produtos,
        pessoas=pessoas,
        formas=FORMAS_PAGAMENTO,
        hoje=date.today().isoformat(),
    )


@app.route("/contas")
def contas():
    conn = get_connection()
    busca = request.args.get("q", "").strip()

    query = """
        SELECT pe.id, pe.nome,
               COALESCE(SUM(p.valor_total) FILTER (WHERE NOT p.pago), 0) AS pendente,
               COALESCE(SUM(p.valor_total) FILTER (WHERE p.pago), 0) AS pago,
               COUNT(*) FILTER (WHERE NOT p.pago) AS itens_pendentes
        FROM pessoas pe
        LEFT JOIN pedidos p ON p.pessoa_id = pe.id
    """
    params = []
    if busca:
        query += " WHERE pe.nome ILIKE %s"
        params.append(f"%{busca}%")
    query += " GROUP BY pe.id, pe.nome ORDER BY pendente DESC, pe.nome"

    with conn.cursor() as cur:
        cur.execute(query, params)
        pessoas = cur.fetchall()
    conn.close()
    return render_template("contas.html", pessoas=pessoas, busca=busca)


@app.route("/contas/<int:pessoa_id>")
def conta_pessoa(pessoa_id):
    conn = get_connection()
    with conn.cursor() as cur:
        cur.execute("SELECT * FROM pessoas WHERE id = %s", (pessoa_id,))
        pessoa = cur.fetchone()
        cur.execute(
            """SELECT p.*, pr.nome AS produto
               FROM pedidos p JOIN produtos pr ON pr.id = p.produto_id
               WHERE p.pessoa_id = %s
               ORDER BY p.data DESC, p.id DESC""",
            (pessoa_id,),
        )
        pedidos = cur.fetchall()
    conn.close()
    if not pessoa:
        flash("Pessoa nao encontrada.", "erro")
        return redirect(url_for("contas"))
    return render_template("conta_pessoa.html", pessoa=pessoa, pedidos=pedidos,
                            hoje=date.today().isoformat(), formas=FORMAS_PAGAMENTO)


@app.route("/pedidos/<int:pedido_id>/pagar", methods=["POST"])
def marcar_pago(pedido_id):
    conn = get_connection()
    forma = request.form.get("forma_pagamento") or "Pix"
    data_pagto = request.form.get("data") or date.today().isoformat()
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE pedidos SET pago = TRUE, data_pagamento = %s, forma_pagamento = %s WHERE id = %s",
            (data_pagto, forma, pedido_id),
        )
        conn.commit()
        cur.execute("SELECT pessoa_id FROM pedidos WHERE id = %s", (pedido_id,))
        pessoa_id = cur.fetchone()["pessoa_id"]
    conn.close()
    return redirect(request.referrer or url_for("conta_pessoa", pessoa_id=pessoa_id))


@app.route("/contas/<int:pessoa_id>/pagar-tudo", methods=["POST"])
def pagar_tudo(pessoa_id):
    conn = get_connection()
    forma = request.form.get("forma_pagamento") or "Pix"
    data_pagto = request.form.get("data") or date.today().isoformat()
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE pedidos SET pago = TRUE, data_pagamento = %s, forma_pagamento = %s "
            "WHERE pessoa_id = %s AND NOT pago",
            (data_pagto, forma, pessoa_id),
        )
    conn.commit()
    conn.close()
    flash("Todas as contas em aberto foram marcadas como pagas.", "ok")
    return redirect(url_for("conta_pessoa", pessoa_id=pessoa_id))


@app.route("/pedidos/<int:pedido_id>/excluir", methods=["POST"])
def excluir_pedido(pedido_id):
    conn = get_connection()
    with conn.cursor() as cur:
        cur.execute("SELECT pessoa_id FROM pedidos WHERE id = %s", (pedido_id,))
        pessoa_id = cur.fetchone()["pessoa_id"]
        cur.execute("DELETE FROM pedidos WHERE id = %s", (pedido_id,))
    conn.commit()
    conn.close()
    flash("Lancamento excluido.", "ok")
    return redirect(url_for("conta_pessoa", pessoa_id=pessoa_id))


@app.route("/cardapio", methods=["GET", "POST"])
def cardapio():
    conn = get_connection()
    if request.method == "POST":
        acao = request.form.get("acao")
        with conn.cursor() as cur:
            if acao == "novo":
                numero_item = request.form.get("numero_item", type=int)
                nome = request.form.get("nome", "").strip()
                preco = request.form.get("preco", type=float)
                if not nome or preco is None:
                    flash("Preencha nome e preco do produto.", "erro")
                else:
                    try:
                        cur.execute(
                            "INSERT INTO produtos (numero_item, nome, preco) VALUES (%s, %s, %s)",
                            (numero_item, nome, preco),
                        )
                        conn.commit()
                        flash(f"Produto '{nome}' adicionado.", "ok")
                    except Exception:
                        conn.rollback()
                        flash("Ja existe um produto com esse numero de item.", "erro")
            elif acao == "editar":
                produto_id = request.form.get("produto_id", type=int)
                preco = request.form.get("preco", type=float)
                cur.execute("UPDATE produtos SET preco = %s WHERE id = %s", (preco, produto_id))
                conn.commit()
                flash("Preco atualizado.", "ok")
            elif acao == "desativar":
                produto_id = request.form.get("produto_id", type=int)
                cur.execute("UPDATE produtos SET ativo = FALSE WHERE id = %s", (produto_id,))
                conn.commit()
                flash("Produto removido do cardapio.", "ok")
        conn.close()
        return redirect(url_for("cardapio"))

    with conn.cursor() as cur:
        cur.execute("SELECT * FROM produtos WHERE ativo ORDER BY numero_item")
        produtos = cur.fetchall()
    conn.close()
    return render_template("cardapio.html", produtos=produtos)


@app.route("/exportar")
def exportar_backup():
    caminho = exportar()
    return send_file(caminho, as_attachment=True)


if __name__ == "__main__":
    import os
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False, threaded=True)
