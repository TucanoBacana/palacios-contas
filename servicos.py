"""Regras compartilhadas entre o app web e o bot: data/hora no fuso do Brasil,
pagamento parcial, desfazer lancamento, historico de alteracoes, mensagem de cobranca
e resumo do dia.
"""
import os
import re
import uuid
from datetime import datetime
from zoneinfo import ZoneInfo

FUSO = ZoneInfo("America/Sao_Paulo")
ESTOQUE_MINIMO = int(os.environ.get("ESTOQUE_MINIMO", "5") or 5)
JANELA_DESFAZER_MIN = 120


def agora():
    return datetime.now(FUSO)


def hoje():
    """Data de hoje no Brasil (o servidor do Render roda em UTC e viraria o dia as 21h)."""
    return agora().date()


def brl(valor):
    valor = float(valor or 0)
    texto = f"{abs(valor):,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")
    return f"{'-' if valor < 0 else ''}R$ {texto}"


def qtd_texto(valor):
    valor = float(valor or 0)
    return str(int(valor)) if valor == int(valor) else f"{valor:.1f}".replace(".", ",")


def novo_lote():
    return uuid.uuid4().hex[:12]


def centavos(valor):
    return int(round(float(valor) * 100))


# ---------------------------------------------------------------- historico

def registrar_historico(cur, quem, acao, detalhe=""):
    cur.execute("INSERT INTO historico (quem, acao, detalhe) VALUES (%s, %s, %s)",
                (quem or "sistema", acao, detalhe))


# ---------------------------------------------------------------- pagamentos

def pendencia_pessoa(cur, pessoa_id):
    """(total em aberto, quantidade de lancamentos em aberto) de uma pessoa."""
    cur.execute(
        """SELECT COALESCE(SUM(valor_total - valor_pago), 0) AS t, COUNT(*) AS c
           FROM pedidos WHERE pessoa_id = %s AND NOT pago""",
        (pessoa_id,),
    )
    r = cur.fetchone()
    return round(float(r["t"]), 2), r["c"]


def aplicar_pagamento(cur, pessoa_id, valor, forma, data, quem, marca=""):
    """Abate `valor` da conta da pessoa, dos lancamentos mais antigos para os mais novos.
    valor=None quita tudo. Um lancamento so vira "pago" quando o valor pago cobre o total dele.
    Retorna dict(pago, quitados, restante). Levanta ValueError se o valor nao for valido.
    """
    cur.execute(
        """SELECT id, valor_total, valor_pago FROM pedidos
           WHERE pessoa_id = %s AND NOT pago ORDER BY data, id FOR UPDATE""",
        (pessoa_id,),
    )
    abertos = cur.fetchall()
    total_c = sum(centavos(p["valor_total"]) - centavos(p["valor_pago"]) for p in abertos)
    if total_c <= 0:
        raise ValueError("Essa pessoa não tem nada em aberto.")
    valor_c = total_c if valor is None else centavos(valor)
    if valor_c <= 0:
        raise ValueError("Informe um valor maior que zero.")
    if valor_c > total_c:
        raise ValueError(f"O valor é maior que a dívida ({brl(total_c / 100)}).")

    sobra, quitados = valor_c, 0
    for p in abertos:
        if sobra <= 0:
            break
        falta = centavos(p["valor_total"]) - centavos(p["valor_pago"])
        if sobra >= falta:
            cur.execute(
                """UPDATE pedidos SET pago = TRUE, valor_pago = valor_total, data_pagamento = %s,
                          forma_pagamento = %s,
                          observacoes = CASE WHEN %s = '' THEN observacoes
                                             ELSE COALESCE(observacoes || ' ', '') || %s END
                   WHERE id = %s""",
                (data, forma, marca, marca, p["id"]),
            )
            sobra -= falta
            quitados += 1
        else:
            cur.execute(
                "UPDATE pedidos SET valor_pago = valor_pago + %s, forma_pagamento = %s WHERE id = %s",
                (sobra / 100, forma, p["id"]),
            )
            sobra = 0
    cur.execute(
        """INSERT INTO pagamentos (data, pessoa_id, valor, forma_pagamento, criado_por)
           VALUES (%s, %s, %s, %s, %s)""",
        (data, pessoa_id, valor_c / 100, forma, quem),
    )
    return {"pago": valor_c / 100, "quitados": quitados, "restante": (total_c - valor_c) / 100}


def pagar_pedido(cur, pedido_id, forma, data, quem):
    """Quita o que falta de um unico lancamento."""
    cur.execute("SELECT pessoa_id, valor_total, valor_pago, pago FROM pedidos WHERE id = %s FOR UPDATE",
                (pedido_id,))
    p = cur.fetchone()
    if not p or p["pago"]:
        return None
    falta = round(p["valor_total"] - p["valor_pago"], 2)
    cur.execute(
        """UPDATE pedidos SET pago = TRUE, valor_pago = valor_total, data_pagamento = %s,
                  forma_pagamento = %s WHERE id = %s""",
        (data, forma, pedido_id),
    )
    cur.execute(
        """INSERT INTO pagamentos (data, pessoa_id, valor, forma_pagamento, criado_por)
           VALUES (%s, %s, %s, %s, %s)""",
        (data, p["pessoa_id"], falta, forma, quem),
    )
    return {"pessoa_id": p["pessoa_id"], "valor": falta}


# ---------------------------------------------------------------- desfazer

def ultimo_lancamento(cur, criado_por):
    """Itens do ultimo lancamento (mesmo lote) feito por esta pessoa na janela de tempo."""
    cur.execute(
        """SELECT lote FROM pedidos
           WHERE criado_por = %s AND lote IS NOT NULL
             AND criado_em > NOW() - (%s || ' minutes')::interval
           ORDER BY id DESC LIMIT 1""",
        (criado_por, str(JANELA_DESFAZER_MIN)),
    )
    r = cur.fetchone()
    if not r:
        return []
    cur.execute(
        """SELECT p.id, p.produto_id, p.quantidade, p.valor_total, p.valor_pago, p.pessoa_id,
                  pe.nome AS pessoa, pr.nome AS produto
           FROM pedidos p JOIN pessoas pe ON pe.id = p.pessoa_id
           JOIN produtos pr ON pr.id = p.produto_id
           WHERE p.lote = %s ORDER BY p.id""",
        (r["lote"],),
    )
    return cur.fetchall()


def texto_ultimo(itens):
    if not itens:
        return None
    total = sum(i["valor_total"] for i in itens)
    linhas = [f"Último lançamento de *{itens[0]['pessoa']}*:"]
    linhas += [f"• {qtd_texto(i['quantidade'])}× {i['produto']}: {brl(i['valor_total'])}" for i in itens]
    linhas.append(f"Total: {brl(total)}")
    return "\n".join(linhas)


def desfazer_itens(cur, itens, quem):
    """Apaga os lancamentos e devolve o estoque. Retorna texto, ou levanta ValueError."""
    if any(i["valor_pago"] > 0 for i in itens):
        raise ValueError("Esse lançamento já recebeu pagamento. Para desfazer, use o app.")
    for i in itens:
        cur.execute("DELETE FROM pedidos WHERE id = %s", (i["id"],))
        cur.execute("UPDATE produtos SET estoque = estoque + %s WHERE id = %s",
                    (i["quantidade"], i["produto_id"]))
    total = sum(i["valor_total"] for i in itens)
    registrar_historico(cur, quem, "Desfez lançamento",
                        f"{itens[0]['pessoa']}: {len(itens)} item(ns), {brl(total)}")
    return f"*Desfeito*: {len(itens)} {'item' if len(itens) == 1 else 'itens'} de *{itens[0]['pessoa']}* " \
           f"({brl(total)}) removidos e o estoque voltou."


# ---------------------------------------------------------------- textos

def primeiro_nome(nome):
    limpo = re.sub(r"[()]", " ", nome or "").split()
    return limpo[0].title() if limpo else ""


def texto_cobranca(cur, pessoa):
    """Mensagem pronta para mandar a quem esta devendo. None se nao deve nada."""
    cur.execute(
        """SELECT p.data, p.quantidade, pr.nome AS produto, (p.valor_total - p.valor_pago) AS falta
           FROM pedidos p JOIN produtos pr ON pr.id = p.produto_id
           WHERE p.pessoa_id = %s AND NOT p.pago ORDER BY p.data, p.id""",
        (pessoa["id"],),
    )
    linhas = cur.fetchall()
    if not linhas:
        return None
    total = sum(l["falta"] for l in linhas)
    partes = [f"Oi, {primeiro_nome(pessoa['nome'])}! Tudo bem?",
              f"Passando para lembrar da sua conta no Palacio's: *{brl(total)}* "
              f"em {len(linhas)} {'item' if len(linhas) == 1 else 'itens'}.", ""]
    for l in linhas[:12]:
        partes.append(f"• {l['data']:%d/%m} {qtd_texto(l['quantidade'])}× {l['produto']}: {brl(l['falta'])}")
    if len(linhas) > 12:
        partes.append(f"… e mais {len(linhas) - 12} itens.")
    pix = os.environ.get("COBRANCA_PIX", "").strip()
    if pix:
        partes += ["", f"Pix: {pix}"]
    partes += ["", "Obrigado!"]
    return "\n".join(partes)


def texto_resumo(cur):
    dia = hoje()
    cur.execute(
        """SELECT pr.categoria, COALESCE(SUM(p.valor_total), 0) AS valor, COUNT(*) AS n
           FROM pedidos p JOIN produtos pr ON pr.id = p.produto_id
           WHERE p.data = %s GROUP BY pr.categoria""",
        (dia,),
    )
    por_cat = {r["categoria"]: r for r in cur.fetchall()}
    vendido = sum(r["valor"] for r in por_cat.values())
    itens = sum(r["n"] for r in por_cat.values())
    cur.execute("SELECT COALESCE(SUM(valor), 0) AS t FROM pagamentos WHERE data = %s", (dia,))
    recebido = cur.fetchone()["t"]
    cur.execute("SELECT COALESCE(SUM(valor_total - valor_pago), 0) AS t, COUNT(DISTINCT pessoa_id) AS n "
                "FROM pedidos WHERE NOT pago")
    aberto = cur.fetchone()
    cur.execute(
        """SELECT pe.nome, SUM(p.valor_total - p.valor_pago) AS total
           FROM pedidos p JOIN pessoas pe ON pe.id = p.pessoa_id
           WHERE NOT p.pago GROUP BY pe.id, pe.nome ORDER BY total DESC LIMIT 3"""
    )
    top = cur.fetchall()
    cur.execute("SELECT nome, estoque FROM produtos WHERE ativo AND estoque <= %s ORDER BY estoque, nome LIMIT 10",
                (ESTOQUE_MINIMO,))
    baixo = cur.fetchall()

    linhas = [f"*Resumo de hoje ({dia:%d/%m})*"]
    if itens:
        det = []
        if "mercadinho" in por_cat:
            det.append(f"Mercadinho {brl(por_cat['mercadinho']['valor'])}")
        if "restaurante" in por_cat:
            det.append(f"Restaurante {brl(por_cat['restaurante']['valor'])}")
        linhas.append(f"Vendido: *{brl(vendido)}* em {itens} {'item' if itens == 1 else 'itens'} ({', '.join(det)})")
    else:
        linhas.append("Nenhuma venda registrada hoje.")
    linhas.append(f"Recebido hoje: *{brl(recebido)}*")
    linhas.append(f"Em aberto no total: *{brl(aberto['t'])}* ({aberto['n']} "
                  f"{'pessoa' if aberto['n'] == 1 else 'pessoas'})")
    if top:
        linhas.append("Quem mais deve: " + ", ".join(f"{t['nome']} ({brl(t['total'])})" for t in top))
    if baixo:
        linhas.append("Estoque baixo: " + ", ".join(f"{b['nome']} ({qtd_texto(b['estoque'])})" for b in baixo))
    return "\n".join(linhas)


# ---------------------------------------------------------------- estoque

def metricas_estoque(cur):
    """Quanto ha de estoque, quanto custou, quanto vale e quanto sobra de lucro se tudo for vendido.
    Valores so contam produtos com custo cadastrado (sem custo nao ha como saber o lucro)."""
    cur.execute(
        """SELECT categoria, COUNT(*) AS skus, COALESCE(SUM(estoque), 0) AS unidades,
                  COALESCE(SUM(estoque * custo), 0) AS investido, COALESCE(SUM(estoque * preco), 0) AS potencial
           FROM produtos WHERE ativo AND estoque > 0 AND custo > 0 GROUP BY categoria ORDER BY categoria"""
    )
    por_categoria = [dict(r, lucro=r["potencial"] - r["investido"]) for r in cur.fetchall()]
    cur.execute("SELECT COUNT(*) AS skus, COALESCE(SUM(estoque), 0) AS unidades FROM produtos "
                "WHERE ativo AND estoque > 0")
    todos = cur.fetchone()
    cur.execute("SELECT COUNT(*) AS n FROM produtos WHERE ativo AND estoque > 0 AND custo <= 0")
    sem_custo = cur.fetchone()["n"]
    cur.execute(
        """SELECT nome, estoque, (preco - custo) * estoque AS lucro FROM produtos
           WHERE ativo AND estoque > 0 AND custo > 0 ORDER BY lucro DESC, nome LIMIT 5"""
    )
    top = cur.fetchall()
    investido = sum(c["investido"] for c in por_categoria)
    potencial = sum(c["potencial"] for c in por_categoria)
    lucro = potencial - investido
    return {
        "investido": investido, "potencial": potencial, "lucro": lucro,
        "margem": (lucro / potencial * 100) if potencial else 0,
        "markup": (lucro / investido * 100) if investido else 0,
        "unidades": todos["unidades"], "skus": todos["skus"], "sem_custo": sem_custo,
        "por_categoria": por_categoria, "top": top,
    }
