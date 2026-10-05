import json
import os
from datetime import date, datetime, timedelta

from flask import Flask, Response, flash, redirect, render_template, request, send_file, url_for
from werkzeug.exceptions import HTTPException
from werkzeug.middleware.proxy_fix import ProxyFix

from db import CATEGORIAS, get_connection, get_or_create_pessoa, init_db
from export_xlsx import exportar
import telegram_bot
import whatsapp_bot

FORMAS_PAGAMENTO = ["Pix", "Dinheiro", "Cartão", "Transferência", "Outro"]
ESTOQUE_BAIXO = 5

app = Flask(__name__)
app.secret_key = "palacios-contas"  # uso interno, sem dados sensiveis
app.wsgi_app = ProxyFix(app.wsgi_app, x_proto=1, x_host=1)

init_db()


def _brl(valor):
    valor = float(valor or 0)
    texto = f"{abs(valor):,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")
    return f"{'-' if valor < 0 else ''}R$ {texto}"


@app.template_filter("brl")
def filtro_brl(valor):
    return _brl(valor)


@app.template_filter("qtd")
def filtro_qtd(valor):
    valor = float(valor or 0)
    return str(int(valor)) if valor == int(valor) else f"{valor:.1f}".replace(".", ",")


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
            """SELECT pe.id, pe.nome, SUM(p.valor_total) AS total
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
            (date.today().replace(day=1).isoformat(),),
        )
        mes = cur.fetchone()
    conn.close()

    lucro_mes = mes["faturamento"] - mes["custo"]

    return render_template(
        "dashboard.html", totais=totais, ranking=ranking, ultimos=ultimos,
        estoque_baixo=estoque_baixo, lucro_mes=lucro_mes,
    )


def _registrar_venda(categoria, template):
    conn = get_connection()

    if request.method == "POST":
        pessoa_nome = request.form.get("pessoa", "").strip()
        forma_pagamento = request.form.get("forma_pagamento") or "Pix"
        pago = request.form.get("pago") == "on"
        observacoes = request.form.get("observacoes", "").strip() or None
        data_lanc = request.form.get("data") or date.today().isoformat()

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
                            custo_unitario, custo_total, forma_pagamento, pago, data_pagamento,
                            observacoes)
                           VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)""",
                        (data_lanc, pessoa_id, item["produto_id"], quantidade, valor_unitario,
                         valor_total, custo_unitario, custo_total, forma_pagamento, pago,
                         data_pagamento, observacoes),
                    )
                    cur.execute(
                        "UPDATE produtos SET estoque = estoque - %s WHERE id = %s",
                        (quantidade, item["produto_id"]),
                    )
                    total_geral += valor_total
                    qtd_itens += 1
            conn.commit()
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
        hoje=date.today().isoformat(),
    )


@app.route("/mercadinho", methods=["GET", "POST"])
def mercadinho():
    return _registrar_venda("mercadinho", "venda.html")


@app.route("/restaurante", methods=["GET", "POST"])
def restaurante():
    return _registrar_venda("restaurante", "venda.html")


@app.route("/estoque", methods=["GET", "POST"])
def estoque():
    conn = get_connection()

    if request.method == "POST":
        produto_id = request.form.get("produto_id", type=int)
        quantidade = request.form.get("quantidade", type=float)
        observacoes = request.form.get("observacoes", "").strip() or None
        data_entrada = request.form.get("data") or date.today().isoformat()

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
                    "UPDATE produtos SET estoque = estoque + %s WHERE id = %s",
                    (quantidade, produto_id),
                )
            conn.commit()
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
            """SELECT p.*, pr.nome AS produto, pr.categoria
               FROM pedidos p JOIN produtos pr ON pr.id = p.produto_id
               WHERE p.pessoa_id = %s
               ORDER BY p.data DESC, p.id DESC""",
            (pessoa_id,),
        )
        pedidos = cur.fetchall()
    conn.close()
    if not pessoa:
        flash("Pessoa não encontrada.", "erro")
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
        cur.execute(
            "SELECT pessoa_id, produto_id, quantidade FROM pedidos WHERE id = %s", (pedido_id,)
        )
        pedido = cur.fetchone()
        if pedido:
            cur.execute("DELETE FROM pedidos WHERE id = %s", (pedido_id,))
            cur.execute(
                "UPDATE produtos SET estoque = estoque + %s WHERE id = %s",
                (pedido["quantidade"], pedido["produto_id"]),
            )
    conn.commit()
    conn.close()
    if not pedido:
        flash("Lançamento não encontrado.", "erro")
        return redirect(url_for("contas"))
    flash("Lançamento excluído e estoque devolvido.", "ok")
    return redirect(url_for("conta_pessoa", pessoa_id=pedido["pessoa_id"]))


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
                        conn.commit()
                        flash(f"Produto '{nome}' adicionado.", "ok")
                    except Exception:
                        conn.rollback()
                        flash("Já existe um produto com esse número de item.", "erro")
            elif acao == "editar":
                produto_id = request.form.get("produto_id", type=int)
                preco = request.form.get("preco", type=float)
                custo = request.form.get("custo", type=float) or 0
                cur.execute(
                    "UPDATE produtos SET preco = %s, custo = %s WHERE id = %s",
                    (preco, custo, produto_id),
                )
                conn.commit()
                flash("Produto atualizado.", "ok")
            elif acao == "desativar":
                produto_id = request.form.get("produto_id", type=int)
                cur.execute("UPDATE produtos SET ativo = FALSE WHERE id = %s", (produto_id,))
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


def _calcular_periodo(req):
    periodo = req.args.get("periodo", "mes")
    hoje = date.today()
    if periodo == "hoje":
        inicio, fim = hoje, hoje
    elif periodo == "7dias":
        inicio, fim = hoje - timedelta(days=6), hoje
    elif periodo == "personalizado":
        data_inicio = req.args.get("data_inicio") or hoje.replace(day=1).isoformat()
        data_fim = req.args.get("data_fim") or hoje.isoformat()
        return periodo, data_inicio, data_fim
    else:
        periodo = "mes"
        inicio, fim = hoje.replace(day=1), hoje
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
                 COALESCE(SUM(p.valor_total) FILTER (WHERE p.pago), 0) AS recebido,
                 COALESCE(SUM(p.valor_total) FILTER (WHERE NOT p.pago), 0) AS a_receber,
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
        por_categoria=por_categoria, top_produtos=top_produtos,
    )


@app.route("/exportar")
def exportar_backup():
    caminho = exportar()
    return send_file(caminho, as_attachment=True)


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
    conn.close()
    return render_template(
        "bot.html", config_telegram=config_telegram, config_whatsapp=config_whatsapp,
        mensagens=mensagens, usuarios_telegram=len(telegram_bot.usuarios_permitidos()),
        webhook_whatsapp=url_for("whatsapp_webhook", _external=True),
    )


@app.route("/bot/telegram/ativar", methods=["POST"])
def telegram_ativar():
    if not os.environ.get("TELEGRAM_TOKEN") or not os.environ.get("TELEGRAM_WEBHOOK_SECRET"):
        flash("Falta configurar TELEGRAM_TOKEN e TELEGRAM_WEBHOOK_SECRET no Render.", "erro")
    else:
        resp = telegram_bot.ativar_webhook(url_for("telegram_webhook", _external=True))
        if resp and resp.get("ok"):
            flash("Telegram conectado: as mensagens agora chegam ao app.", "ok")
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
        if info.get("pending_update_count"):
            partes.append(f"{info['pending_update_count']} mensagens esperando.")
        if info.get("last_error_message"):
            partes.append("Último erro: " + info["last_error_message"])
        flash(" ".join(partes), "erro" if info.get("last_error_message") else "ok")
    return redirect(url_for("bot"))


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


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False, threaded=True)
