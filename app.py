import hashlib
import io
import json
import logging
import os
import threading
from datetime import date, datetime, timedelta

from flask import (Flask, Response, flash, redirect, render_template, request, send_file,
                   send_from_directory, url_for)
from werkzeug.exceptions import HTTPException
from werkzeug.middleware.proxy_fix import ProxyFix

import notificacoes
import servicos
import telegram_bot
import whatsapp_bot
from db import CATEGORIAS, DATABASE_URL, get_connection, get_or_create_pessoa, init_db
from export_xlsx import exportar_bytes
from servicos import brl as _brl
from servicos import hoje

FORMAS_PAGAMENTO = ["Pix", "Dinheiro", "Cartão", "Transferência", "Outro"]
ESTOQUE_BAIXO = servicos.ESTOQUE_MINIMO

# chave das mensagens de aviso (flash): derivada do banco, para nao ser um texto publico no codigo
_semente = os.environ.get("SECRET_KEY") or hashlib.sha256(f"palacios|{DATABASE_URL or ''}".encode()).hexdigest()

app = Flask(__name__)
app.secret_key = _semente
app.wsgi_app = ProxyFix(app.wsgi_app, x_proto=1, x_host=1)

init_db()


# ------------------------------------------------------------------ filtros

@app.template_filter("brl")
def filtro_brl(valor):
    return _brl(valor)


@app.template_filter("qtd")
def filtro_qtd(valor):
    return servicos.qtd_texto(valor)


@app.template_filter("pct")
def filtro_pct(valor, casas=0):
    return f"{float(valor or 0):.{casas}f}".replace(".", ",") + "%"


@app.template_filter("data_br")
def filtro_data_br(valor):
    if isinstance(valor, (date, datetime)):
        return valor.strftime("%d/%m/%Y")
    if isinstance(valor, str) and len(valor) == 10 and valor[4] == "-" and valor[7] == "-":
        return f"{valor[8:10]}/{valor[5:7]}/{valor[0:4]}"
    return valor or ""


def usuario_atual():
    """Quem fez a acao no site (nao ha login, entao e sempre "App"; o bot registra o nome de quem usou)."""
    return "App"


# ------------------------------------------------------------------ painel

@app.route("/")
def dashboard():
    conn = get_connection()
    with conn.cursor() as cur:
        cur.execute(
            """SELECT
                 COALESCE(SUM(valor_total - valor_pago) FILTER (WHERE NOT pago), 0) AS total_pendente,
                 COALESCE(SUM(valor_pago), 0) AS total_pago,
                 COUNT(*) FILTER (WHERE NOT pago) AS qtd_pendente
               FROM pedidos"""
        )
        totais = cur.fetchone()

        cur.execute(
            """SELECT pe.id, pe.nome, SUM(p.valor_total - p.valor_pago) AS total
               FROM pedidos p JOIN pessoas pe ON pe.id = p.pessoa_id
               WHERE NOT p.pago
               GROUP BY pe.id, pe.nome
               ORDER BY total DESC
               LIMIT 8"""
        )
        ranking = cur.fetchall()

        cur.execute(
            """SELECT p.id, p.data, pe.id AS pessoa_id, pe.nome AS pessoa, pr.nome AS produto, pr.categoria,
                      p.quantidade, p.valor_total, p.pago
               FROM pedidos p
               JOIN pessoas pe ON pe.id = p.pessoa_id
               JOIN produtos pr ON pr.id = p.produto_id
               ORDER BY p.id DESC
               LIMIT 10"""
        )
        ultimos = cur.fetchall()

        cur.execute(
            """SELECT nome, categoria, estoque FROM produtos
               WHERE ativo AND estoque <= %s
               ORDER BY estoque ASC, nome
               LIMIT 12""",
            (ESTOQUE_BAIXO,),
        )
        estoque_baixo = cur.fetchall()

        cur.execute(
            """SELECT COALESCE(SUM(valor_total), 0) AS faturamento,
                      COALESCE(SUM(custo_total), 0) AS custo
               FROM pedidos WHERE data >= %s""",
            (hoje().replace(day=1).isoformat(),),
        )
        mes = cur.fetchone()
        estoque_info = servicos.metricas_estoque(cur)
    conn.close()

    lucro_mes = mes["faturamento"] - mes["custo"]

    return render_template(
        "dashboard.html", totais=totais, ranking=ranking, ultimos=ultimos,
        estoque_baixo=estoque_baixo, lucro_mes=lucro_mes, estoque_info=estoque_info,
    )


# ------------------------------------------------------------------ vendas

def _avisar_estoque(conn):
    """Alerta de estoque baixo pelo Telegram; nunca deixa um erro daqui quebrar a venda."""
    try:
        notificacoes.checar_estoque(conn)
    except Exception:
        conn.rollback()
        app.logger.exception("Falha ao checar o estoque")


def _registrar_venda(categoria, template):
    conn = get_connection()

    if request.method == "POST":
        pessoa_nome = request.form.get("pessoa", "").strip()
        forma_pagamento = request.form.get("forma_pagamento") or "Pix"
        pago = request.form.get("pago") == "on"
        observacoes = request.form.get("observacoes", "").strip() or None
        data_lanc = request.form.get("data") or hoje().isoformat()

        try:
            itens_brutos = json.loads(request.form.get("itens") or "[]")
            itens = []
            for i in itens_brutos:
                produto_id = int(i.get("produto_id"))
                quantidade = float(i.get("quantidade") or 0)
                if produto_id and quantidade > 0:
                    itens.append({"produto_id": produto_id, "quantidade": quantidade})
        except (ValueError, TypeError, AttributeError):
            itens = []

        erro = None
        if not pessoa_nome:
            erro = "Informe o nome da pessoa."
        elif not itens:
            erro = "Selecione ao menos um produto (toque nos itens para adicionar)."

        if erro:
            flash(erro, "erro")
        else:
            pessoa_id = get_or_create_pessoa(conn, pessoa_nome)
            total_geral = 0.0
            qtd_itens = 0
            lote = servicos.novo_lote()
            quem = usuario_atual()
            with conn.cursor() as cur:
                for item in itens:
                    cur.execute("SELECT * FROM produtos WHERE id = %s", (item["produto_id"],))
                    produto = cur.fetchone()
                    if not produto:
                        continue
                    quantidade = item["quantidade"]
                    valor_unitario = produto["preco"]
                    valor_total = round(valor_unitario * quantidade, 2)
                    custo_unitario = produto["custo"] or 0
                    custo_total = round(custo_unitario * quantidade, 2)
                    data_pagamento = data_lanc if pago else None

                    cur.execute(
                        """INSERT INTO pedidos
                           (data, pessoa_id, produto_id, quantidade, valor_unitario, valor_total,
                            custo_unitario, custo_total, forma_pagamento, pago, valor_pago,
                            data_pagamento, observacoes, lote, criado_por)
                           VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)""",
                        (data_lanc, pessoa_id, item["produto_id"], quantidade, valor_unitario,
                         valor_total, custo_unitario, custo_total, forma_pagamento, pago,
                         valor_total if pago else 0, data_pagamento, observacoes, lote, "App"),
                    )
                    cur.execute(
                        "UPDATE produtos SET estoque = estoque - %s WHERE id = %s",
                        (quantidade, item["produto_id"]),
                    )
                    total_geral += valor_total
                    qtd_itens += 1
                if pago and total_geral:
                    cur.execute(
                        """INSERT INTO pagamentos (data, pessoa_id, valor, forma_pagamento, criado_por)
                           VALUES (%s, %s, %s, %s, %s)""",
                        (data_lanc, pessoa_id, round(total_geral, 2), forma_pagamento, "App"),
                    )
                servicos.registrar_historico(
                    cur, quem, "Registrou venda",
                    f"{pessoa_nome}: {qtd_itens} item(ns), {_brl(total_geral)}" + (" (já pago)" if pago else ""))
            conn.commit()
            _avisar_estoque(conn)
            conn.close()
            flash(f"Registrado para {pessoa_nome}: {qtd_itens} item(ns), {_brl(total_geral)}", "ok")
            return redirect(url_for(request.endpoint))

    with conn.cursor() as cur:
        cur.execute(
            "SELECT * FROM produtos WHERE ativo AND categoria = %s ORDER BY nome",
            (categoria,),
        )
        produtos = cur.fetchall()
        cur.execute("SELECT nome FROM pessoas ORDER BY nome")
        pessoas = cur.fetchall()
    conn.close()

    return render_template(
        template,
        produtos=produtos,
        pessoas=pessoas,
        formas=FORMAS_PAGAMENTO,
        hoje=hoje().isoformat(),
    )


@app.route("/mercadinho", methods=["GET", "POST"])
def mercadinho():
    return _registrar_venda("mercadinho", "venda.html")


@app.route("/restaurante", methods=["GET", "POST"])
def restaurante():
    return _registrar_venda("restaurante", "venda.html")


@app.route("/estoque/<int:produto_id>/contagem", methods=["POST"])
def contar_estoque(produto_id):
    """Confirma a quantidade real de um produto cujo estoque estava estimado."""
    contado = request.form.get("contagem", type=float)
    if contado is None or contado < 0:
        flash("Digite a quantidade contada (zero ou mais).", "erro")
        return redirect(url_for("estoque"))
    conn = get_connection()
    with conn.cursor() as cur:
        cur.execute("SELECT nome, estoque FROM produtos WHERE id = %s FOR UPDATE", (produto_id,))
        prod = cur.fetchone()
        if prod:
            delta = round(contado - prod["estoque"], 4)
            if delta:
                cur.execute(
                    """INSERT INTO entradas_estoque (data, produto_id, quantidade, observacoes)
                       VALUES (%s, %s, %s, %s)""",
                    (hoje().isoformat(), produto_id, delta,
                     f"Ajuste de contagem: estava {servicos.qtd_texto(prod['estoque'])}, "
                     f"contado {servicos.qtd_texto(contado)}"))
            cur.execute("UPDATE produtos SET estoque = %s, estoque_estimado = FALSE WHERE id = %s",
                        (contado, produto_id))
            servicos.registrar_historico(
                cur, usuario_atual(), "Contagem de estoque",
                f"{prod['nome']}: {servicos.qtd_texto(prod['estoque'])} → {servicos.qtd_texto(contado)}")
    conn.commit()
    _avisar_estoque(conn)
    conn.close()
    flash("Contagem confirmada." if prod else "Produto não encontrado.", "ok" if prod else "erro")
    return redirect(url_for("estoque"))


@app.route("/estoque", methods=["GET", "POST"])
def estoque():
    conn = get_connection()

    if request.method == "POST":
        produto_id = request.form.get("produto_id", type=int)
        quantidade = request.form.get("quantidade", type=float)
        observacoes = request.form.get("observacoes", "").strip() or None
        data_entrada = request.form.get("data") or hoje().isoformat()

        if not produto_id or not quantidade or quantidade <= 0:
            flash("Selecione o produto e informe uma quantidade válida.", "erro")
        else:
            with conn.cursor() as cur:
                cur.execute(
                    """INSERT INTO entradas_estoque (data, produto_id, quantidade, observacoes)
                       VALUES (%s, %s, %s, %s)""",
                    (data_entrada, produto_id, quantidade, observacoes),
                )
                cur.execute(
                    "UPDATE produtos SET estoque = estoque + %s WHERE id = %s RETURNING nome",
                    (quantidade, produto_id),
                )
                produto = cur.fetchone()
                servicos.registrar_historico(
                    cur, usuario_atual(), "Entrada de estoque",
                    f"{produto['nome'] if produto else produto_id}: +{servicos.qtd_texto(quantidade)}")
            conn.commit()
            _avisar_estoque(conn)  # repor libera um novo aviso quando cair de novo
            flash("Estoque atualizado.", "ok")
        conn.close()
        return redirect(url_for("estoque"))

    with conn.cursor() as cur:
        cur.execute("SELECT * FROM produtos WHERE ativo ORDER BY categoria, nome")
        produtos = cur.fetchall()
        cur.execute(
            """SELECT e.*, p.nome AS produto, p.categoria
               FROM entradas_estoque e JOIN produtos p ON p.id = e.produto_id
               ORDER BY e.id DESC LIMIT 20"""
        )
        entradas = cur.fetchall()
    conn.close()

    return render_template(
        "estoque.html",
        mercadinho_produtos=[p for p in produtos if p["categoria"] == "mercadinho"],
        restaurante_produtos=[p for p in produtos if p["categoria"] == "restaurante"],
        entradas=entradas,
        hoje=hoje().isoformat(),
    )


# ------------------------------------------------------------------ contas

@app.route("/contas")
def contas():
    conn = get_connection()
    busca = request.args.get("q", "").strip()

    query = """
        SELECT pe.id, pe.nome,
               COALESCE(SUM(p.valor_total - p.valor_pago) FILTER (WHERE NOT p.pago), 0) AS pendente,
               COALESCE(SUM(p.valor_pago), 0) AS pago,
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
            """SELECT p.*, pr.nome AS produto, pr.categoria
               FROM pedidos p JOIN produtos pr ON pr.id = p.produto_id
               WHERE p.pessoa_id = %s
               ORDER BY p.data DESC, p.id DESC""",
            (pessoa_id,),
        )
        pedidos = cur.fetchall()
        cobranca = servicos.texto_cobranca(cur, pessoa) if pessoa else None
        cur.execute(
            "SELECT data, valor, forma_pagamento FROM pagamentos WHERE pessoa_id = %s "
            "ORDER BY data DESC, id DESC LIMIT 8", (pessoa_id,))
        pagamentos = cur.fetchall()
    conn.close()
    if not pessoa:
        flash("Pessoa não encontrada.", "erro")
        return redirect(url_for("contas"))
    return render_template("conta_pessoa.html", pessoa=pessoa, pedidos=pedidos, cobranca=cobranca,
                           pagamentos=pagamentos, hoje=hoje().isoformat(), formas=FORMAS_PAGAMENTO)


@app.route("/pedidos/<int:pedido_id>/pagar", methods=["POST"])
def marcar_pago(pedido_id):
    conn = get_connection()
    forma = request.form.get("forma_pagamento") or "Pix"
    data_pagto = request.form.get("data") or hoje().isoformat()
    with conn.cursor() as cur:
        feito = servicos.pagar_pedido(cur, pedido_id, forma, data_pagto, usuario_atual())
        if feito:
            cur.execute("SELECT nome FROM pessoas WHERE id = %s", (feito["pessoa_id"],))
            servicos.registrar_historico(cur, usuario_atual(), "Marcou como pago",
                                         f"{cur.fetchone()['nome']}: {_brl(feito['valor'])} ({forma})")
    conn.commit()
    conn.close()
    if not feito:
        flash("Esse lançamento não está mais em aberto.", "erro")
        return redirect(request.referrer or url_for("contas"))
    return redirect(request.referrer or url_for("conta_pessoa", pessoa_id=feito["pessoa_id"]))


def _pagar(pessoa_id, valor):
    conn = get_connection()
    forma = request.form.get("forma_pagamento") or "Pix"
    data_pagto = request.form.get("data") or hoje().isoformat()
    with conn.cursor() as cur:
        cur.execute("SELECT nome FROM pessoas WHERE id = %s", (pessoa_id,))
        pessoa = cur.fetchone()
        try:
            r = servicos.aplicar_pagamento(cur, pessoa_id, valor, forma, data_pagto, usuario_atual())
            servicos.registrar_historico(cur, usuario_atual(), "Registrou pagamento",
                                         f"{pessoa['nome']}: {_brl(r['pago'])} ({forma})")
            conn.commit()
            if r["restante"] > 0:
                flash(f"Pagamento de {_brl(r['pago'])} registrado. Ainda falta {_brl(r['restante'])}.", "ok")
            else:
                flash("Conta quitada: todos os lançamentos foram marcados como pagos.", "ok")
        except ValueError as erro:
            conn.rollback()
            flash(str(erro), "erro")
    conn.close()
    return redirect(url_for("conta_pessoa", pessoa_id=pessoa_id))


@app.route("/contas/<int:pessoa_id>/pagar-tudo", methods=["POST"])
def pagar_tudo(pessoa_id):
    return _pagar(pessoa_id, None)


@app.route("/contas/<int:pessoa_id>/pagar-parte", methods=["POST"])
def pagar_parte(pessoa_id):
    valor = whatsapp_bot.ler_valor(request.form.get("valor", ""))
    if valor is None or valor <= 0:
        flash("Digite o valor pago, por exemplo 20 ou 20,50.", "erro")
        return redirect(url_for("conta_pessoa", pessoa_id=pessoa_id))
    return _pagar(pessoa_id, valor)


@app.route("/pedidos/<int:pedido_id>/excluir", methods=["POST"])
def excluir_pedido(pedido_id):
    conn = get_connection()
    with conn.cursor() as cur:
        cur.execute(
            """SELECT p.pessoa_id, p.produto_id, p.quantidade, p.valor_total, pe.nome AS pessoa,
                      pr.nome AS produto
               FROM pedidos p JOIN pessoas pe ON pe.id = p.pessoa_id
               JOIN produtos pr ON pr.id = p.produto_id WHERE p.id = %s""",
            (pedido_id,),
        )
        pedido = cur.fetchone()
        if pedido:
            cur.execute("DELETE FROM pedidos WHERE id = %s", (pedido_id,))
            cur.execute(
                "UPDATE produtos SET estoque = estoque + %s WHERE id = %s",
                (pedido["quantidade"], pedido["produto_id"]),
            )
            servicos.registrar_historico(
                cur, usuario_atual(), "Excluiu lançamento",
                f"{pedido['pessoa']}: {servicos.qtd_texto(pedido['quantidade'])}× {pedido['produto']}, "
                f"{_brl(pedido['valor_total'])}")
    conn.commit()
    conn.close()
    if not pedido:
        flash("Lançamento não encontrado.", "erro")
        return redirect(url_for("contas"))
    flash("Lançamento excluído e estoque devolvido.", "ok")
    return redirect(url_for("conta_pessoa", pessoa_id=pedido["pessoa_id"]))


@app.route("/pedidos/<int:pedido_id>/editar", methods=["GET", "POST"])
def editar_pedido(pedido_id):
    conn = get_connection()
    with conn.cursor() as cur:
        cur.execute(
            """SELECT p.*, pe.nome AS pessoa, pr.nome AS produto
               FROM pedidos p JOIN pessoas pe ON pe.id = p.pessoa_id
               JOIN produtos pr ON pr.id = p.produto_id WHERE p.id = %s FOR UPDATE OF p""",
            (pedido_id,),
        )
        pedido = cur.fetchone()
        if not pedido:
            conn.close()
            flash("Lançamento não encontrado.", "erro")
            return redirect(url_for("contas"))
        # com pagamento ja recebido, mexer em produto/quantidade desmontaria o valor pago
        travado = pedido["valor_pago"] > 0

        if request.method == "POST":
            pessoa_id = request.form.get("pessoa_id", type=int) or pedido["pessoa_id"]
            observacoes = request.form.get("observacoes", "").strip() or None
            try:
                data_nova = date.fromisoformat(request.form.get("data", "")).isoformat()
            except ValueError:
                data_nova = pedido["data"].isoformat()
            cur.execute("SELECT nome FROM pessoas WHERE id = %s", (pessoa_id,))
            pessoa_nova = cur.fetchone()
            if not pessoa_nova:
                pessoa_id, pessoa_nova = pedido["pessoa_id"], {"nome": pedido["pessoa"]}

            produto_id, quantidade = pedido["produto_id"], pedido["quantidade"]
            valor_unit, custo_unit = pedido["valor_unitario"], pedido["custo_unitario"]
            erro = None
            if not travado:
                quantidade = request.form.get("quantidade", type=float) or 0
                produto_id = request.form.get("produto_id", type=int) or pedido["produto_id"]
                if quantidade <= 0:
                    erro = "Informe uma quantidade maior que zero."
                elif produto_id != pedido["produto_id"]:
                    cur.execute("SELECT preco, custo FROM produtos WHERE id = %s", (produto_id,))
                    novo = cur.fetchone()
                    if not novo:
                        erro = "Produto não encontrado."
                    else:
                        valor_unit, custo_unit = novo["preco"], novo["custo"] or 0
            if erro:
                flash(erro, "erro")
                conn.rollback()
            else:
                if produto_id != pedido["produto_id"] or quantidade != pedido["quantidade"]:
                    cur.execute("UPDATE produtos SET estoque = estoque + %s WHERE id = %s",
                                (pedido["quantidade"], pedido["produto_id"]))
                    cur.execute("UPDATE produtos SET estoque = estoque - %s WHERE id = %s",
                                (quantidade, produto_id))
                valor_total = round(valor_unit * quantidade, 2)
                cur.execute(
                    """UPDATE pedidos SET pessoa_id = %s, produto_id = %s, quantidade = %s,
                              valor_unitario = %s, valor_total = %s, custo_unitario = %s,
                              custo_total = %s, data = %s, observacoes = %s WHERE id = %s""",
                    (pessoa_id, produto_id, quantidade, valor_unit, valor_total, custo_unit,
                     round(custo_unit * quantidade, 2), data_nova, observacoes, pedido_id),
                )
                cur.execute("SELECT nome FROM produtos WHERE id = %s", (produto_id,))
                servicos.registrar_historico(
                    cur, usuario_atual(), "Editou lançamento",
                    f"era {pedido['pessoa']}: {servicos.qtd_texto(pedido['quantidade'])}× {pedido['produto']} "
                    f"({_brl(pedido['valor_total'])}); agora {pessoa_nova['nome']}: "
                    f"{servicos.qtd_texto(quantidade)}× {cur.fetchone()['nome']} ({_brl(valor_total)})")
                conn.commit()
                conn.close()
                _verificar_estoque_depois()
                flash("Lançamento atualizado.", "ok")
                return redirect(url_for("conta_pessoa", pessoa_id=pessoa_id))

        cur.execute("SELECT id, nome FROM pessoas ORDER BY nome")
        pessoas = cur.fetchall()
        cur.execute("SELECT id, nome, categoria FROM produtos WHERE ativo OR id = %s ORDER BY categoria, nome",
                    (pedido["produto_id"],))
        produtos = cur.fetchall()
    conn.close()
    return render_template("editar_pedido.html", pedido=pedido, pessoas=pessoas, produtos=produtos,
                           travado=travado)


def _verificar_estoque_depois():
    conn = get_connection()
    try:
        _avisar_estoque(conn)
    finally:
        conn.close()


# ------------------------------------------------------------------ cardapio e custos

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
                custo = request.form.get("custo", type=float) or 0
                categoria = request.form.get("categoria") or "mercadinho"
                if categoria not in CATEGORIAS:
                    categoria = "mercadinho"
                estoque_inicial = request.form.get("estoque_inicial", type=float) or 0
                if not nome or preco is None:
                    flash("Preencha o nome e o preço do produto.", "erro")
                else:
                    try:
                        cur.execute(
                            """INSERT INTO produtos (numero_item, nome, preco, custo, categoria, estoque)
                               VALUES (%s, %s, %s, %s, %s, %s)""",
                            (numero_item, nome, preco, custo, categoria, estoque_inicial),
                        )
                        servicos.registrar_historico(cur, usuario_atual(), "Novo produto",
                                                     f"{nome}: {_brl(preco)}")
                        conn.commit()
                        flash(f"Produto '{nome}' adicionado.", "ok")
                    except Exception:
                        conn.rollback()
                        flash("Já existe um produto com esse número de item.", "erro")
            elif acao == "editar":
                produto_id = request.form.get("produto_id", type=int)
                preco = request.form.get("preco", type=float)
                custo = request.form.get("custo", type=float) or 0
                cur.execute("SELECT nome, preco, custo FROM produtos WHERE id = %s", (produto_id,))
                antes = cur.fetchone()
                cur.execute(
                    "UPDATE produtos SET preco = %s, custo = %s WHERE id = %s",
                    (preco, custo, produto_id),
                )
                if antes and (antes["preco"] != preco or antes["custo"] != custo):
                    servicos.registrar_historico(
                        cur, usuario_atual(), "Editou produto",
                        f"{antes['nome']}: preço {_brl(antes['preco'])} → {_brl(preco)}, "
                        f"custo {_brl(antes['custo'])} → {_brl(custo)}")
                conn.commit()
                flash("Produto atualizado.", "ok")
            elif acao == "desativar":
                produto_id = request.form.get("produto_id", type=int)
                cur.execute("UPDATE produtos SET ativo = FALSE WHERE id = %s RETURNING nome", (produto_id,))
                produto = cur.fetchone()
                if produto:
                    servicos.registrar_historico(cur, usuario_atual(), "Removeu produto", produto["nome"])
                conn.commit()
                flash("Produto removido do cardápio.", "ok")
        conn.close()
        return redirect(url_for("cardapio"))

    with conn.cursor() as cur:
        cur.execute("SELECT * FROM produtos WHERE ativo ORDER BY categoria, nome")
        produtos = cur.fetchall()
    conn.close()
    return render_template(
        "cardapio.html",
        mercadinho=[p for p in produtos if p["categoria"] == "mercadinho"],
        restaurante=[p for p in produtos if p["categoria"] == "restaurante"],
    )


def _numero(texto):
    try:
        return float(str(texto).replace(",", "."))
    except (TypeError, ValueError):
        return None


@app.route("/custos", methods=["GET", "POST"])
def custos():
    conn = get_connection()
    if request.method == "POST":
        retroativo = request.form.get("retroativo") == "on"
        alterados, vendas = 0, 0
        with conn.cursor() as cur:
            cur.execute("SELECT id, nome, custo FROM produtos WHERE ativo")
            for prod in cur.fetchall():
                novo = _numero(request.form.get(f"custo_{prod['id']}"))
                if novo is None or novo < 0:
                    continue
                novo = round(novo, 4)  # custo de compra pode ter ate 4 casas (ex.: R$ 0,1958 por unidade)
                if novo != round(prod["custo"] or 0, 4):
                    cur.execute("UPDATE produtos SET custo = %s WHERE id = %s", (novo, prod["id"]))
                    alterados += 1
                if retroativo and novo > 0:
                    # so as vendas antigas que ficaram sem custo; o que ja tinha custo guardado nao muda
                    cur.execute(
                        """UPDATE pedidos SET custo_unitario = %s, custo_total = ROUND((quantidade * %s)::numeric, 2)
                           WHERE produto_id = %s AND custo_unitario = 0""",
                        (novo, novo, prod["id"]),
                    )
                    vendas += cur.rowcount
            if alterados or vendas:
                servicos.registrar_historico(cur, usuario_atual(), "Atualizou custos",
                                             f"{alterados} produto(s); {vendas} venda(s) antigas recalculadas")
        conn.commit()
        conn.close()
        if alterados or vendas:
            flash(f"Custos salvos: {alterados} produto(s) atualizado(s)"
                  + (f" e {vendas} venda(s) antigas recalculadas." if vendas else "."), "ok")
        else:
            flash("Nada mudou.", "ok")
        return redirect(url_for("custos"))

    with conn.cursor() as cur:
        cur.execute("SELECT * FROM produtos WHERE ativo ORDER BY categoria, nome")
        produtos = cur.fetchall()
    conn.close()
    return render_template(
        "custos.html",
        mercadinho=[p for p in produtos if p["categoria"] == "mercadinho"],
        restaurante=[p for p in produtos if p["categoria"] == "restaurante"],
        sem_custo=sum(1 for p in produtos if (p["custo"] or 0) <= 0),
    )


# ------------------------------------------------------------------ financeiro e historico

def _calcular_periodo(req):
    periodo = req.args.get("periodo", "mes")
    dia = hoje()
    if periodo == "hoje":
        inicio, fim = dia, dia
    elif periodo == "7dias":
        inicio, fim = dia - timedelta(days=6), dia
    elif periodo == "personalizado":
        data_inicio = req.args.get("data_inicio") or dia.replace(day=1).isoformat()
        data_fim = req.args.get("data_fim") or dia.isoformat()
        return periodo, data_inicio, data_fim
    else:
        periodo = "mes"
        inicio, fim = dia.replace(day=1), dia
    return periodo, inicio.isoformat(), fim.isoformat()


@app.route("/financeiro")
def financeiro():
    conn = get_connection()
    periodo, data_inicio, data_fim = _calcular_periodo(request)
    categoria = request.args.get("categoria", "todas")
    if categoria not in CATEGORIAS:
        categoria = "todas"

    with conn.cursor() as cur:
        cur.execute(
            """SELECT
                 COALESCE(SUM(p.valor_total), 0) AS faturamento,
                 COALESCE(SUM(p.custo_total), 0) AS custo,
                 COALESCE(SUM(p.valor_pago), 0) AS recebido,
                 COALESCE(SUM(p.valor_total - p.valor_pago), 0) AS a_receber,
                 COUNT(*) AS qtd_vendas
               FROM pedidos p
               JOIN produtos pr ON pr.id = p.produto_id
               WHERE p.data BETWEEN %s AND %s
                 AND (%s = 'todas' OR pr.categoria = %s)""",
            (data_inicio, data_fim, categoria, categoria),
        )
        totais = cur.fetchone()

        cur.execute(
            """SELECT pr.categoria,
                 COALESCE(SUM(p.valor_total), 0) AS faturamento,
                 COALESCE(SUM(p.custo_total), 0) AS custo
               FROM pedidos p JOIN produtos pr ON pr.id = p.produto_id
               WHERE p.data BETWEEN %s AND %s
               GROUP BY pr.categoria""",
            (data_inicio, data_fim),
        )
        por_categoria = cur.fetchall()

        cur.execute(
            """SELECT pr.nome, pr.categoria, SUM(p.quantidade) AS qtd_vendida,
                 SUM(p.valor_total) AS faturamento, SUM(p.valor_total - p.custo_total) AS lucro
               FROM pedidos p JOIN produtos pr ON pr.id = p.produto_id
               WHERE p.data BETWEEN %s AND %s
                 AND (%s = 'todas' OR pr.categoria = %s)
               GROUP BY pr.id, pr.nome, pr.categoria
               ORDER BY lucro DESC
               LIMIT 10""",
            (data_inicio, data_fim, categoria, categoria),
        )
        top_produtos = cur.fetchall()
        cur.execute("SELECT COUNT(*) AS n FROM produtos WHERE ativo AND custo <= 0")
        sem_custo = cur.fetchone()["n"]
    conn.close()

    faturamento = totais["faturamento"]
    custo = totais["custo"]
    lucro = faturamento - custo
    margem = (lucro / faturamento * 100) if faturamento else 0

    for row in por_categoria:
        row["lucro"] = row["faturamento"] - row["custo"]
        row["margem"] = (row["lucro"] / row["faturamento"] * 100) if row["faturamento"] else 0

    return render_template(
        "financeiro.html",
        periodo=periodo, data_inicio=data_inicio, data_fim=data_fim, categoria=categoria,
        faturamento=faturamento, custo=custo, lucro=lucro, margem=margem, totais=totais,
        por_categoria=por_categoria, top_produtos=top_produtos, sem_custo=sem_custo,
    )


@app.route("/historico")
def historico():
    conn = get_connection()
    with conn.cursor() as cur:
        cur.execute(
            "SELECT quem, acao, detalhe, "
            "(criado_em AT TIME ZONE 'UTC') AT TIME ZONE 'America/Sao_Paulo' AS quando "
            "FROM historico ORDER BY id DESC LIMIT 200"
        )
        registros = cur.fetchall()
    conn.close()
    return render_template("historico.html", registros=registros)


@app.route("/exportar")
def exportar_backup():
    return send_file(io.BytesIO(exportar_bytes()), as_attachment=True,
                     download_name=f"palacios_backup_{hoje():%Y%m%d}.xlsx",
                     mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")


# ------------------------------------------------------------------ saude, offline, app instalavel

@app.route("/saude")
def saude():
    """Endereco para o monitor gratuito (UptimeRobot) acessar a cada 5 minutos:
    mantem o app acordado e dispara o resumo do dia, o backup semanal e o alerta de estoque."""
    try:
        conn = get_connection()
        with conn.cursor() as cur:
            cur.execute("INSERT INTO avisos (chave) VALUES ('ping') "
                        "ON CONFLICT (chave) DO UPDATE SET criado_em = NOW()")
        conn.commit()
        conn.close()
        notificacoes.rodar_tarefas(get_connection)
    except Exception:
        app.logger.exception("Falha no /saude")
        return Response("erro", status=500, mimetype="text/plain")
    return Response("ok", mimetype="text/plain")


@app.route("/offline")
def offline():
    return render_template("offline.html")


@app.route("/sw.js")
def service_worker():
    resp = send_from_directory(app.static_folder, "sw.js", mimetype="application/javascript")
    resp.headers["Cache-Control"] = "no-cache"
    return resp


# ------------------------------------------------------------------ bots

@app.route("/whatsapp/webhook", methods=["GET", "POST"])
def whatsapp_webhook():
    if request.method == "GET":
        esperado = os.environ.get("WHATSAPP_VERIFY_TOKEN")
        if (esperado and request.args.get("hub.mode") == "subscribe"
                and request.args.get("hub.verify_token") == esperado):
            return Response(request.args.get("hub.challenge", ""), mimetype="text/plain")
        return Response("token invalido", status=403)

    if not whatsapp_bot.assinatura_valida(request.get_data(), request.headers.get("X-Hub-Signature-256")):
        return Response("assinatura invalida", status=403)
    whatsapp_bot.processar_webhook(request.get_json(silent=True) or {}, get_connection)
    return Response("ok", status=200)


@app.route("/telegram/webhook", methods=["POST"])
def telegram_webhook():
    if not telegram_bot.assinatura_valida(request.headers.get("X-Telegram-Bot-Api-Secret-Token")):
        return Response("assinatura invalida", status=403)
    telegram_bot.processar_update(request.get_json(silent=True) or {}, get_connection)
    return Response("ok", status=200)


@app.route("/whatsapp")
def whatsapp():
    return redirect(url_for("bot"))


@app.route("/bot")
def bot():
    def tem(nome):
        return bool(os.environ.get(nome))

    config_telegram = [
        ("Token do bot (TELEGRAM_TOKEN)", tem("TELEGRAM_TOKEN")),
        ("Frase secreta (TELEGRAM_WEBHOOK_SECRET)", tem("TELEGRAM_WEBHOOK_SECRET")),
        ("Usuários autorizados (TELEGRAM_USUARIOS)", bool(telegram_bot.usuarios_permitidos())),
    ]
    config_whatsapp = [
        ("Token de acesso (WHATSAPP_TOKEN)", tem("WHATSAPP_TOKEN")),
        ("ID do número (WHATSAPP_PHONE_ID)", tem("WHATSAPP_PHONE_ID")),
        ("Código de verificação (WHATSAPP_VERIFY_TOKEN)", tem("WHATSAPP_VERIFY_TOKEN")),
        ("Chave secreta do app (WHATSAPP_APP_SECRET)", tem("WHATSAPP_APP_SECRET")),
        ("Números autorizados (WHATSAPP_NUMEROS)", bool(whatsapp_bot.numeros_permitidos())),
    ]
    conn = get_connection()
    with conn.cursor() as cur:
        cur.execute(
            "SELECT canal, nome, telefone, texto, resposta, "
            "(criado_em AT TIME ZONE 'UTC') AT TIME ZONE 'America/Sao_Paulo' AS criado_em "
            "FROM whatsapp_mensagens ORDER BY criado_em DESC LIMIT 25"
        )
        mensagens = cur.fetchall()
        cur.execute("SELECT EXTRACT(EPOCH FROM (NOW() - criado_em)) AS seg FROM avisos WHERE chave = 'ping'")
        ping = cur.fetchone()
        cur.execute("SELECT MAX(criado_em AT TIME ZONE 'UTC' AT TIME ZONE 'America/Sao_Paulo') AS quando "
                    "FROM avisos WHERE chave LIKE 'backup:%'")
        ultimo_backup = cur.fetchone()["quando"]
    conn.close()
    minutos_ping = int(ping["seg"] // 60) if ping else None
    return render_template(
        "bot.html", config_telegram=config_telegram, config_whatsapp=config_whatsapp,
        mensagens=mensagens, usuarios_telegram=len(telegram_bot.usuarios_permitidos()),
        webhook_whatsapp=url_for("whatsapp_webhook", _external=True),
        url_saude=url_for("saude", _external=True), minutos_ping=minutos_ping,
        avisos_ativos=notificacoes.ativo(), ultimo_backup=ultimo_backup,
        resumo_hora=os.environ.get("RESUMO_HORA", "20"), estoque_minimo=ESTOQUE_BAIXO,
    )


@app.route("/bot/telegram/ativar", methods=["POST"])
def telegram_ativar():
    if not os.environ.get("TELEGRAM_TOKEN") or not os.environ.get("TELEGRAM_WEBHOOK_SECRET"):
        flash("Falta configurar TELEGRAM_TOKEN e TELEGRAM_WEBHOOK_SECRET no Render.", "erro")
    else:
        resp = telegram_bot.ativar_webhook(url_for("telegram_webhook", _external=True))
        if resp and resp.get("ok"):
            flash("Telegram conectado: as mensagens e os botões agora chegam ao app.", "ok")
        else:
            flash("O Telegram recusou: " + str((resp or {}).get("description", "sem resposta")), "erro")
    return redirect(url_for("bot"))


@app.route("/bot/telegram/verificar", methods=["POST"])
def telegram_verificar():
    eu = telegram_bot.chamar("getMe")
    if eu is None:
        flash("Falta configurar TELEGRAM_TOKEN no Render.", "erro")
    elif not eu.get("ok"):
        flash("Token inválido: " + str(eu.get("description")), "erro")
    else:
        info = (telegram_bot.chamar("getWebhookInfo") or {}).get("result", {})
        partes = [f"Bot @{eu['result'].get('username')} encontrado."]
        partes.append("Webhook ativo." if info.get("url") else "Webhook ainda não ativado.")
        if "callback_query" not in (info.get("allowed_updates") or ["callback_query"]):
            partes.append("Os botões ainda não estão liberados: clique em Conectar Telegram ao app.")
        if info.get("pending_update_count"):
            partes.append(f"{info['pending_update_count']} mensagens esperando.")
        if info.get("last_error_message"):
            partes.append("Último erro: " + info["last_error_message"])
        flash(" ".join(partes), "erro" if info.get("last_error_message") else "ok")
    return redirect(url_for("bot"))


@app.route("/bot/avisos/teste", methods=["POST"])
def avisos_teste():
    if not notificacoes.ativo():
        flash("Falta o TELEGRAM_TOKEN ou um usuário em TELEGRAM_USUARIOS no Render.", "erro")
    else:
        conn = get_connection()
        with conn.cursor() as cur:
            texto = servicos.texto_resumo(cur)
        conn.close()
        flash("Resumo enviado no Telegram." if notificacoes.avisar(texto)
              else "O Telegram não aceitou o envio. Abra o bot e mande /start para ele.", "ok")
    return redirect(url_for("bot"))


@app.route("/bot/backup", methods=["POST"])
def backup_telegram():
    if not notificacoes.ativo():
        flash("Falta o TELEGRAM_TOKEN ou um usuário em TELEGRAM_USUARIOS no Render.", "erro")
    elif notificacoes.enviar_backup():
        flash("Backup enviado no Telegram.", "ok")
    else:
        flash("O Telegram não aceitou o envio. Abra o bot e mande /start para ele.", "erro")
    return redirect(url_for("bot"))


# ------------------------------------------------------------------ erros

@app.errorhandler(404)
def pagina_nao_encontrada(e):
    return render_template(
        "erro.html", codigo=404, titulo="Página não encontrada",
        mensagem="Esse endereço não existe. Confira o link ou volte ao Painel.",
    ), 404


@app.errorhandler(500)
@app.errorhandler(Exception)
def erro_interno(e):
    if isinstance(e, HTTPException):
        return e
    app.logger.exception("Erro nao tratado")
    return render_template(
        "erro.html", codigo=500, titulo="Algo deu errado",
        mensagem="Não deu para concluir essa ação. Tente de novo em alguns segundos; "
                 "se continuar, avise quem cuida do sistema.",
    ), 500


# ------------------------------------------------------------------ inicio

def _ativar_telegram_no_inicio():
    """O Render informa o endereco publico em RENDER_EXTERNAL_URL. Reativar o webhook a cada
    inicio garante que os botoes (callback_query) fiquem liberados sem passo manual."""
    base = os.environ.get("RENDER_EXTERNAL_URL")
    if (not base or not os.environ.get("TELEGRAM_TOKEN") or not os.environ.get("TELEGRAM_WEBHOOK_SECRET")
            or os.environ.get("TELEGRAM_AUTO_WEBHOOK") == "0"):
        return
    try:
        telegram_bot.ativar_webhook(base.rstrip("/") + "/telegram/webhook")
    except Exception:
        logging.getLogger("telegram").exception("Nao consegui ativar o webhook no inicio")


threading.Thread(target=_ativar_telegram_no_inicio, daemon=True).start()


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False, threaded=True)
