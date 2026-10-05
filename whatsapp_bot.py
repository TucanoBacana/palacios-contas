"""Bot do WhatsApp: le mensagens em texto livre ("Kevin 2 coxinhas e 1 guarana"),
mostra um resumo, pede confirmacao e so entao grava no app.

Variaveis de ambiente (configuradas no Render, nunca no codigo):
  WHATSAPP_TOKEN          token de acesso da Meta (para responder)
  WHATSAPP_PHONE_ID       "Phone Number ID" do numero do bot
  WHATSAPP_VERIFY_TOKEN   texto qualquer, igual ao digitado na configuracao do webhook
  WHATSAPP_APP_SECRET     "Chave secreta do app" (valida que a mensagem veio da Meta)
  WHATSAPP_NUMEROS        quem pode usar o bot: 5511999990000:Yan,5521988880000:Maria
"""
import difflib
import hashlib
import hmac
import json
import logging
import os
import re
import unicodedata
import urllib.error
import urllib.request
from datetime import date

from db import get_or_create_pessoa

log = logging.getLogger("whatsapp")

VALIDADE_CONVERSA_MIN = 15
VERSAO_API = os.environ.get("WHATSAPP_API_VERSION", "v21.0")

CONFIRMAR = {"sim", "s", "ok", "confirmo", "confirma", "confirmar", "pode", "isso", "certo",
             "correto", "beleza", "blz", "registra", "registrar", "manda", "yes"}
CANCELAR = {"nao", "n", "cancela", "cancelar", "cancel", "errado", "esquece", "deixa"}
PALAVRAS_CONSULTA = {"quanto", "saldo", "deve", "devendo", "divida", "devedor", "devedores",
                     "conta", "total", "resumo", "extrato", "pendente", "pendentes", "quem"}
RUIDO_CONSULTA = PALAVRAS_CONSULTA | {"de", "do", "da", "o", "a", "esta", "e", "me", "mostra",
                                       "mostrar", "ver", "qual", "que", "tem", "geral", "hoje",
                                       "por", "favor", "pfv", "agora", "ainda", "tudo", "todos"}
NUMEROS = {"um": "1", "uma": "1", "dois": "2", "duas": "2", "tres": "3", "quatro": "4",
           "cinco": "5", "seis": "6", "sete": "7", "oito": "8", "nove": "9", "dez": "10"}
STOP = {"de", "da", "do", "das", "dos", "e", "com", "a", "o", "as", "os"}
UNIDADE = re.compile(r"^\d+(ml|mm|l|g|kg)$")

MENU = (
    "*PALACIO'S* - o que você quer fazer?\n\n"
    "1) Registrar compra\n"
    "2) Consultar a conta de alguém\n"
    "3) Ver quem está devendo\n"
    "4) Registrar pagamento\n\n"
    "Responda com o *número*.\n"
    "Digite *lista* para ver o cardápio.\n"
    "Atalho: você também pode mandar direto, ex.: *Kevin 2 coxinhas*"
)
RODAPE = "\n\nDigite *menu* para voltar ao início."
FORMAS_PAGAMENTO = ["Pix", "Dinheiro", "Cartão", "Transferência"]
MENU_PALAVRAS = {"menu", "start", "inicio", "voltar", "0", "oi", "ola", "ajuda", "help", "comandos",
                 "bom dia", "boa tarde", "boa noite", "como funciona"}
LISTA_PALAVRAS = {"lista", "cardapio", "produtos", "itens"}


# ---------------------------------------------------------------- texto

def sem_acento(txt):
    txt = unicodedata.normalize("NFD", txt or "")
    return "".join(c for c in txt if unicodedata.category(c) != "Mn")


def normalizar(txt):
    return re.sub(r"[^a-z0-9]+", " ", sem_acento(txt).lower()).strip()


def radical(tok):
    return tok[:-1] if len(tok) > 3 and tok.endswith("s") else tok


def tokens_produto(nome):
    return [radical(t) for t in normalizar(nome).split()
            if t not in STOP and not UNIDADE.match(t) and not t.isdigit()]


def tokens_pessoa(nome):
    return [t for t in normalizar(nome).split() if t not in STOP]


def brl(valor):
    valor = float(valor or 0)
    texto = f"{abs(valor):,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")
    return f"{'-' if valor < 0 else ''}R$ {texto}"


def _tok_igual(a, b):
    if a == b:
        return True
    return len(a) >= 4 and len(b) >= 4 and difflib.SequenceMatcher(None, a, b).ratio() >= 0.82


def _pontuar(frase, alvo):
    if not frase or not alvo:
        return 0.0, 0.0
    achados = sum(1 for t in frase if any(_tok_igual(t, a) for a in alvo))
    cobre = sum(1 for a in alvo if any(_tok_igual(t, a) for t in frase))
    return achados / len(frase), cobre / len(alvo)


def _achar(frase, alvos):
    """alvos: lista de (objeto, tokens). Retorna (tipo, valor, total)."""
    cands = []
    for obj, toks in alvos:
        cont, cobre = _pontuar(frase, toks)
        if cont >= 1.0:
            cands.append((cobre, obj))
    if not cands:
        return "nenhum", None, 0
    cands.sort(key=lambda c: -c[0])
    if len(cands) == 1 or cands[0][0] - cands[1][0] >= 0.2:
        return "unico", cands[0][1], len(cands)
    return "ambiguo", [o for _, o in cands[:5]], len(cands)


def resolver_produto(palavras, indice):
    frase = [radical(t) for t in palavras
             if t not in STOP and not UNIDADE.match(t) and not t.isdigit()]
    unidades = [t for t in palavras if UNIDADE.match(t)]
    if not frase:
        return "nenhum", None, 0
    tipo, val, total = _achar(frase, indice)
    if tipo == "ambiguo" and unidades:
        filtrados = [p for p in val if all(u in normalizar(p["nome"]) for u in unidades)]
        if len(filtrados) == 1:
            return "unico", filtrados[0], 1
    return tipo, val, total


def resolver_pessoa(palavras, indice):
    frase = [t for t in palavras if t not in STOP and not t.isdigit()]
    if not frase:
        return "nenhum", None, 0
    return _achar(frase, indice)


# ---------------------------------------------------------- interpretacao

def dividir_em_trechos(texto):
    t = sem_acento(texto).lower()
    t = t.replace("meia duzia", "6")
    t = re.sub(r"\b(uma\s+)?duzia\b", "12", t)
    t = re.sub(r"(\d)\s*x\b", r"\1", t)
    t = re.sub(r"[,;+\n]+", " | ", t)
    t = re.sub(r"\be\b", " | ", t)
    t = re.sub(r"[^a-z0-9|]+", " ", t)
    trechos = []
    for parte in t.split("|"):
        palavras = [NUMEROS.get(p, p) for p in parte.split()]
        if palavras:
            trechos.append(palavras)
    return trechos


def _separar_nome_produto(palavras, idx_prod):
    """Ex.: ['kevin', 'coxinha'] -> (['kevin'], ['coxinha']). Prefere a parte de produto mais longa."""
    melhor = None
    n = len(palavras)
    for k in range(1, n):
        for nome, prod in ((palavras[:k], palavras[k:]), (palavras[k:], palavras[:k])):
            tipo, _, _ = resolver_produto(prod, idx_prod)
            if tipo != "nenhum" and (melhor is None or len(prod) > len(melhor[1])):
                melhor = (nome, prod)
    return melhor


def _compacto_produto(p):
    return {"id": p["id"], "nome": p["nome"], "preco": float(p["preco"])}


def montar_estado(texto, pessoas, produtos):
    """Transforma a frase em um estado de registro. Retorna (estado, nao_entendidos)."""
    trechos = dividir_em_trechos(texto)
    idx_prod = [(p, tokens_produto(p["nome"])) for p in produtos]
    idx_pes = [(p, tokens_pessoa(p["nome"])) for p in pessoas]

    pessoa_palavras = None
    for i, tr in enumerate(trechos):
        for j, w in enumerate(tr):
            if w in ("pro", "pra", "para") and j < len(tr) - 1:
                pessoa_palavras = tr[j + 1:]
                trechos[i] = tr[:j]
                break
        if pessoa_palavras:
            break
    if pessoa_palavras is None and trechos:
        tr = trechos[0]
        pos = next((j for j, w in enumerate(tr) if w.isdigit()), None)
        if pos:
            pessoa_palavras = tr[:pos]
            trechos[0] = tr[pos:]

    itens = []
    nao_entendidos = []
    for tr in trechos:
        qtd, achou, resto = 1, False, []
        for w in tr:
            if w.isdigit():
                if not achou and len(w) <= 2:
                    qtd, achou = int(w), True
            else:
                resto.append(w)
        if not resto or qtd < 1:
            continue
        tipo, val, total = resolver_produto(resto, idx_prod)
        if tipo == "nenhum" and pessoa_palavras is None:
            achado = _separar_nome_produto(resto, idx_prod)
            if achado:
                pessoa_palavras, resto = achado
                tipo, val, total = resolver_produto(resto, idx_prod)
            elif resolver_pessoa(resto, idx_pes)[0] != "nenhum":
                pessoa_palavras = resto
                continue
        if tipo == "nenhum":
            nao_entendidos.append(" ".join(resto))
            continue
        item = {"qtd": qtd, "digitado": " ".join(resto), "produto": None, "total_opcoes": total}
        if tipo == "unico":
            item["produto"] = _compacto_produto(val)
        else:
            item["candidatos"] = [_compacto_produto(p) for p in val]
        itens.append(item)

    pessoa = None
    if pessoa_palavras:
        tipo, val, total = resolver_pessoa(pessoa_palavras, idx_pes)
        digitado = " ".join(pessoa_palavras)
        if tipo == "unico":
            pessoa = {"id": val["id"], "nome": val["nome"]}
        elif tipo == "ambiguo":
            pessoa = {"candidatos": [{"id": p["id"], "nome": p["nome"]} for p in val],
                      "digitado": digitado, "total_opcoes": total}
        else:
            pessoa = {"novo": digitado.upper()}
    return {"pessoa": pessoa, "itens": itens}, nao_entendidos


# ---------------------------------------------------------- conversa

def _opcoes_texto(titulo, opcoes, total, formato):
    linhas = [titulo]
    for i, o in enumerate(opcoes, 1):
        linhas.append(f"{i}) {formato(o)}")
    if total > len(opcoes):
        linhas.append(f"(Há mais {total - len(opcoes)} parecidos. Se não for nenhum, cancele e mande o nome completo.)")
    linhas.append("Responda com o *número*. Digite *menu* para cancelar.")
    return "\n".join(linhas)


def proxima_pergunta(estado):
    pessoa = estado.get("pessoa")
    if not pessoa:
        estado["etapa"] = "pedir_pessoa"
        return "Para quem é? Responda só com o nome."
    if "candidatos" in pessoa:
        estado["etapa"] = "escolher_pessoa"
        return _opcoes_texto(f"Qual pessoa você quis dizer com *{pessoa['digitado']}*?",
                             pessoa["candidatos"], pessoa.get("total_opcoes", 0),
                             lambda p: p["nome"])
    for i, item in enumerate(estado["itens"]):
        if not item.get("produto"):
            estado["etapa"] = "escolher_produto"
            estado["alvo"] = i
            return _opcoes_texto(f"Qual produto você quis dizer com *{item['digitado']}*?",
                                 item["candidatos"], item.get("total_opcoes", 0),
                                 lambda p: f"{p['nome']} ({brl(p['preco'])})")
    return None


def texto_confirmacao(estado):
    pessoa = estado["pessoa"]
    linhas = ["*Confirma?*", f"Pessoa: *{pessoa.get('nome') or pessoa['novo']}*"]
    if "novo" in pessoa:
        linhas.append("Atenção: não achei essa pessoa no cadastro, ela será criada como nova.")
    total = 0.0
    for it in estado["itens"]:
        p = it["produto"]
        subtotal = it["qtd"] * p["preco"]
        total += subtotal
        linhas.append(f"• {it['qtd']}× {p['nome']}: {brl(subtotal)}")
    linhas.append(f"Total: *{brl(total)}* (fica em aberto)")
    linhas.append("1) Sim, registrar\n2) Não, cancelar")
    return "\n".join(linhas)


def continuar(estado, texto, pessoas):
    """Aplica a resposta do usuario ao estado. Retorna 'cancelar'|'confirmado'|'ok'|'novo'."""
    n = normalizar(texto)
    etapa = estado.get("etapa")
    if n in CANCELAR or (etapa == "confirmar" and n == "2"):
        return "cancelar"
    if etapa in ("escolher_pessoa", "escolher_produto", "escolher_consulta"):
        if not re.fullmatch(r"\d{1,2}", n):
            return "novo"
        if etapa == "escolher_pessoa":
            opcoes = estado["pessoa"]["candidatos"]
        elif etapa == "escolher_produto":
            opcoes = estado["itens"][estado["alvo"]]["candidatos"]
        else:
            opcoes = estado["candidatos"]
        i = int(n) - 1
        if not 0 <= i < len(opcoes):
            return "novo"
        if etapa == "escolher_pessoa":
            estado["pessoa"] = {"id": opcoes[i]["id"], "nome": opcoes[i]["nome"]}
        elif etapa == "escolher_produto":
            item = estado["itens"][estado["alvo"]]
            item["produto"] = opcoes[i]
            item.pop("candidatos", None)
        else:
            estado["escolhido"] = opcoes[i]
        return "ok"
    if etapa == "pedir_pessoa":
        palavras = n.split()
        if not palavras or len(palavras) > 4 or any(w.isdigit() for w in palavras):
            return "novo"
        idx = [(p, tokens_pessoa(p["nome"])) for p in pessoas]
        tipo, val, total = resolver_pessoa(palavras, idx)
        if tipo == "unico":
            estado["pessoa"] = {"id": val["id"], "nome": val["nome"]}
        elif tipo == "ambiguo":
            estado["pessoa"] = {"candidatos": [{"id": p["id"], "nome": p["nome"]} for p in val],
                                "digitado": n, "total_opcoes": total}
        else:
            estado["pessoa"] = {"novo": n.upper()}
        return "ok"
    if etapa == "confirmar" and (n in CONFIRMAR or n == "1"):
        return "confirmado"
    return "novo"


# ---------------------------------------------------------- banco

def _carregar_estado(cur, chave):
    cur.execute(
        "SELECT dados FROM whatsapp_estado WHERE chave = %s "
        "AND atualizado_em > NOW() - INTERVAL '%s minutes'" % ("%s", VALIDADE_CONVERSA_MIN),
        (chave,),
    )
    row = cur.fetchone()
    return json.loads(row["dados"]) if row else None


def _salvar_estado(cur, chave, estado):
    cur.execute(
        """INSERT INTO whatsapp_estado (chave, dados, atualizado_em) VALUES (%s, %s, NOW())
           ON CONFLICT (chave) DO UPDATE SET dados = EXCLUDED.dados, atualizado_em = NOW()""",
        (chave, json.dumps(estado)),
    )


def _apagar_estado(cur, chave):
    cur.execute("DELETE FROM whatsapp_estado WHERE chave = %s", (chave,))


def _texto_conta(cur, pessoa):
    cur.execute(
        """SELECT p.data, p.quantidade, pr.nome AS produto, p.valor_total
           FROM pedidos p JOIN produtos pr ON pr.id = p.produto_id
           WHERE p.pessoa_id = %s AND NOT p.pago
           ORDER BY p.data DESC, p.id DESC""",
        (pessoa["id"],),
    )
    linhas = cur.fetchall()
    if not linhas:
        return f"*{pessoa['nome']}* não deve nada."
    total = sum(l["valor_total"] for l in linhas)
    saida = [f"*{pessoa['nome']}* deve *{brl(total)}* em {len(linhas)} "
             f"{'item' if len(linhas) == 1 else 'itens'}:"]
    for l in linhas[:10]:
        qtd = int(l["quantidade"]) if l["quantidade"] == int(l["quantidade"]) else l["quantidade"]
        saida.append(f"• {l['data']:%d/%m} {qtd}× {l['produto']}: {brl(l['valor_total'])}")
    if len(linhas) > 10:
        saida.append(f"… e mais {len(linhas) - 10}.")
    return "\n".join(saida)


def _texto_ranking(cur):
    cur.execute(
        """SELECT pe.nome, SUM(p.valor_total) AS total
           FROM pedidos p JOIN pessoas pe ON pe.id = p.pessoa_id
           WHERE NOT p.pago GROUP BY pe.id, pe.nome ORDER BY total DESC LIMIT 8"""
    )
    linhas = cur.fetchall()
    cur.execute("SELECT COALESCE(SUM(valor_total), 0) AS t FROM pedidos WHERE NOT pago")
    geral = cur.fetchone()["t"]
    if not linhas:
        return "Ninguém está devendo nada agora."
    saida = [f"Falta receber *{brl(geral)}*. Quem mais deve:"]
    for i, l in enumerate(linhas, 1):
        saida.append(f"{i}) {l['nome']}: {brl(l['total'])}")
    return "\n".join(saida)


def _registrar(conn, estado, operador, canal="WhatsApp"):
    cur = conn.cursor()
    pessoa = estado["pessoa"]
    pessoa_id = pessoa["id"] if "id" in pessoa else get_or_create_pessoa(conn, pessoa["novo"])
    nome = pessoa.get("nome") or pessoa["novo"]
    hoje = date.today().isoformat()
    obs = f"via {canal} ({operador})"
    total, avisos, linhas = 0.0, [], []
    for it in estado["itens"]:
        cur.execute("SELECT nome, preco, custo FROM produtos WHERE id = %s", (it["produto"]["id"],))
        prod = cur.fetchone()
        if not prod:
            raise ValueError("produto removido durante a conversa")
        qtd = it["qtd"]
        valor = round(prod["preco"] * qtd, 2)
        custo = round((prod["custo"] or 0) * qtd, 2)
        cur.execute(
            """INSERT INTO pedidos
               (data, pessoa_id, produto_id, quantidade, valor_unitario, valor_total,
                custo_unitario, custo_total, forma_pagamento, pago, data_pagamento, observacoes)
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s, 'Pix', FALSE, NULL, %s)""",
            (hoje, pessoa_id, it["produto"]["id"], qtd, prod["preco"], valor,
             prod["custo"] or 0, custo, obs),
        )
        cur.execute("UPDATE produtos SET estoque = estoque - %s WHERE id = %s RETURNING estoque",
                    (qtd, it["produto"]["id"]))
        sobra = cur.fetchone()["estoque"]
        if sobra < 0:
            avisos.append(f"Atenção: {prod['nome']} ficou com estoque {sobra:g}.")
        total += valor
        linhas.append(f"• {qtd}× {prod['nome']}: {brl(valor)}")
    cur.execute("SELECT COALESCE(SUM(valor_total), 0) AS t FROM pedidos WHERE pessoa_id = %s AND NOT pago",
                (pessoa_id,))
    divida = cur.fetchone()["t"]
    saida = [f"*Registrado* para *{nome}*:"] + linhas
    saida.append(f"Total: *{brl(total)}*. Agora {nome} deve {brl(divida)} no total.")
    return "\n".join(saida + avisos)


def _numero(n, maximo):
    if re.fullmatch(r"\d{1,2}", n) and 1 <= int(n) <= maximo:
        return int(n) - 1
    return None


def _texto_lista(cur):
    cur.execute("SELECT nome, preco, categoria FROM produtos WHERE ativo ORDER BY categoria, nome")
    saida, atual = [], None
    for p in cur.fetchall():
        if p["categoria"] != atual:
            atual = p["categoria"]
            saida.append(f"\n*{'Restaurante' if atual == 'restaurante' else 'Mercadinho'}*")
        saida.append(f"{p['nome']}: {brl(p['preco'])}")
    return "\n".join(saida).strip() or "O cardápio está vazio."


def _nome_pessoa(estado):
    p = estado["pessoa"]
    return p.get("nome") or p["novo"]


def _tentar_pessoa(estado, n, pessoas):
    """Devolve a proxima pergunta, ou None quando estado['pessoa'] ficou definido."""
    palavras = n.split()
    if not palavras or any(w.isdigit() for w in palavras) or len(palavras) > 5:
        estado["etapa"] = "quem"
        return "Digite só o nome da pessoa, por exemplo: *Kevin*."
    idx = [(p, tokens_pessoa(p["nome"])) for p in pessoas]
    tipo, val, total = resolver_pessoa(palavras, idx)
    if tipo == "unico":
        estado["pessoa"] = {"id": val["id"], "nome": val["nome"]}
        return None
    if tipo == "ambiguo":
        estado.update(etapa="quem_escolher", digitado=n, total_opcoes=total,
                      candidatos=[{"id": p["id"], "nome": p["nome"]} for p in val])
        return _opcoes_texto(f"Qual pessoa você quis dizer com *{n}*?", estado["candidatos"],
                             total, lambda p: p["nome"])
    if estado["fluxo"] == "compra":
        estado.update(etapa="quem_novo", novo_nome=n.upper())
        return (f"Não achei *{n}* no cadastro.\n"
                f"1) Criar *{n.upper()}* como pessoa nova\n2) Digitar o nome de novo")
    estado["etapa"] = "quem"
    return f"Não achei ninguém chamado *{n}*. Digite o nome de novo."


PERGUNTA_ITEM = "Qual item? Digite o nome (ex.: *coxinha*) ou *lista* para ver o cardápio."


def _texto_mais(estado):
    total = sum(i["qtd"] * i["produto"]["preco"] for i in estado["itens"])
    linhas = [f"Anotado. Até agora para *{_nome_pessoa(estado)}*:"]
    linhas += [f"• {i['qtd']}× {i['produto']['nome']}" for i in estado["itens"]]
    linhas.append(f"Subtotal: {brl(total)}\n")
    linhas.append("1) Adicionar outro item\n2) Finalizar")
    return "\n".join(linhas)


def _apos_pessoa(cur, chave, estado):
    fluxo = estado["fluxo"]
    if fluxo == "compra":
        estado["etapa"] = "item"
        _salvar_estado(cur, chave, estado)
        return PERGUNTA_ITEM
    if fluxo == "consulta":
        _apagar_estado(cur, chave)
        return _texto_conta(cur, estado["pessoa"]) + RODAPE
    cur.execute("SELECT COALESCE(SUM(valor_total), 0) AS t, COUNT(*) AS c FROM pedidos "
                "WHERE pessoa_id = %s AND NOT pago", (estado["pessoa"]["id"],))
    r = cur.fetchone()
    if r["c"] == 0:
        _apagar_estado(cur, chave)
        return f"*{_nome_pessoa(estado)}* não deve nada." + RODAPE
    estado.update(etapa="forma", pendente=float(r["t"]), qtd_pendente=r["c"])
    _salvar_estado(cur, chave, estado)
    opcoes = "\n".join(f"{i}) {f}" for i, f in enumerate(FORMAS_PAGAMENTO, 1))
    return (f"*{_nome_pessoa(estado)}* deve *{brl(r['t'])}* em {r['c']} "
            f"{'item' if r['c'] == 1 else 'itens'}.\nComo foi o pagamento?\n{opcoes}\n"
            "(A baixa é do valor total. Para pagar só uma parte, use o app.)")


def _dar_baixa(cur, estado, operador, canal="WhatsApp"):
    cur.execute(
        """UPDATE pedidos SET pago = TRUE, data_pagamento = %s, forma_pagamento = %s,
                  observacoes = COALESCE(observacoes || ' ', '') || %s
           WHERE pessoa_id = %s AND NOT pago RETURNING valor_total""",
        (date.today().isoformat(), estado["forma"], f"[baixa via {canal} ({operador})]",
         estado["pessoa"]["id"]),
    )
    linhas = cur.fetchall()
    total = sum(l["valor_total"] for l in linhas)
    return (f"*Baixa registrada*: {_nome_pessoa(estado)} pagou *{brl(total)}* "
            f"({estado['forma']}), {len(linhas)} {'item' if len(linhas) == 1 else 'itens'}.")


def _adicionar_item(estado, produto, qtd):
    estado["itens"].append({"qtd": qtd, "produto": produto})
    estado.pop("atual", None)
    estado["etapa"] = "mais"
    return _texto_mais(estado)


def _passo_fluxo(conn, cur, chave, operador, estado, n, pessoas, idx_produtos, canal="WhatsApp"):
    etapa = estado["etapa"]
    if n in ("cancelar", "cancela", "cancel") or (n in CANCELAR and etapa != "mais"):
        _apagar_estado(cur, chave)
        return "Cancelado. Nada foi registrado." + RODAPE

    def salvar(texto):
        _salvar_estado(cur, chave, estado)
        return texto

    # ----- quem
    if etapa == "quem":
        pergunta = _tentar_pessoa(estado, n, pessoas)
        return salvar(pergunta) if pergunta else _apos_pessoa(cur, chave, estado)
    if etapa == "quem_escolher":
        i = _numero(n, len(estado["candidatos"]))
        if i is None:
            return _opcoes_texto("Responda com o número da pessoa:", estado["candidatos"],
                                 estado.get("total_opcoes", 0), lambda p: p["nome"])
        escolhida = estado["candidatos"][i]
        estado["pessoa"] = {"id": escolhida["id"], "nome": escolhida["nome"]}
        for k in ("candidatos", "digitado", "total_opcoes"):
            estado.pop(k, None)
        return _apos_pessoa(cur, chave, estado)
    if etapa == "quem_novo":
        if n == "1":
            estado["pessoa"] = {"novo": estado.pop("novo_nome")}
            return _apos_pessoa(cur, chave, estado)
        if n == "2":
            estado["etapa"] = "quem"
            return salvar("Digite o nome de novo.")
        return "Responda *1* para criar a pessoa nova ou *2* para digitar o nome de novo."

    # ----- itens
    if etapa == "item":
        if n in LISTA_PALAVRAS:
            return _texto_lista(cur) + "\n\n" + PERGUNTA_ITEM
        qtd, resto = None, []
        for w in n.split():
            w = NUMEROS.get(w, w)
            if w.isdigit():
                if qtd is None and len(w) <= 2 and int(w) >= 1:
                    qtd = int(w)
            else:
                resto.append(w)
        if not resto:
            return "Digite o nome do item, por exemplo *coxinha*. Para ver o cardápio, digite *lista*."
        tipo, val, total = resolver_produto(resto, idx_produtos)
        if tipo == "nenhum":
            return (f"Não achei *{' '.join(resto)}* no cardápio. Digite de novo ou *lista* para "
                    "ver os produtos.")
        if tipo == "ambiguo":
            estado["atual"] = {"candidatos": [_compacto_produto(p) for p in val], "qtd": qtd,
                               "digitado": " ".join(resto), "total_opcoes": total}
            estado["etapa"] = "item_escolher"
            return salvar(_opcoes_texto(f"Qual você quis dizer com *{' '.join(resto)}*?",
                                        estado["atual"]["candidatos"], total,
                                        lambda p: f"{p['nome']} ({brl(p['preco'])})"))
        produto = _compacto_produto(val)
        if qtd:
            return salvar(_adicionar_item(estado, produto, qtd))
        estado["atual"] = {"produto": produto}
        estado["etapa"] = "qtd"
        return salvar(f"Quantas unidades de *{produto['nome']}*? Digite só o número.")
    if etapa == "item_escolher":
        atual = estado["atual"]
        i = _numero(n, len(atual["candidatos"]))
        if i is None:
            return _opcoes_texto("Responda com o número do produto:", atual["candidatos"],
                                 atual.get("total_opcoes", 0),
                                 lambda p: f"{p['nome']} ({brl(p['preco'])})")
        produto = atual["candidatos"][i]
        if atual.get("qtd"):
            return salvar(_adicionar_item(estado, produto, atual["qtd"]))
        estado["atual"] = {"produto": produto}
        estado["etapa"] = "qtd"
        return salvar(f"Quantas unidades de *{produto['nome']}*? Digite só o número.")
    if etapa == "qtd":
        if re.fullmatch(r"\d{1,2}", n) and int(n) >= 1:
            return salvar(_adicionar_item(estado, estado["atual"]["produto"], int(n)))
        return "Digite só o número de unidades, por exemplo *2*."
    if etapa == "mais":
        if n in ("1", "sim", "s", "mais", "outro"):
            estado["etapa"] = "item"
            return salvar(PERGUNTA_ITEM)
        if n in ("2", "nao", "n", "fim", "finalizar", "pronto", "so isso"):
            estado["etapa"] = "confirmar"
            return salvar(texto_confirmacao(estado))
        return "Responda *1* para adicionar outro item ou *2* para finalizar."
    if etapa == "confirmar":
        if n in CONFIRMAR or n == "1":
            _apagar_estado(cur, chave)
            return _registrar(conn, estado, operador, canal) + RODAPE
        if n == "2":
            _apagar_estado(cur, chave)
            return "Cancelado. Nada foi registrado." + RODAPE
        return "Responda *1* para registrar ou *2* para cancelar."

    # ----- pagamento
    if etapa == "forma":
        i = _numero(n, len(FORMAS_PAGAMENTO))
        if i is None:
            return "Responda com o número da forma de pagamento (1 a %d)." % len(FORMAS_PAGAMENTO)
        estado["forma"] = FORMAS_PAGAMENTO[i]
        estado["etapa"] = "pag_confirmar"
        return salvar(f"Dar baixa em *{brl(estado['pendente'])}* de *{_nome_pessoa(estado)}* "
                      f"({estado['forma']})?\n1) Sim, dar baixa\n2) Não, cancelar")
    if etapa == "pag_confirmar":
        if n in CONFIRMAR or n == "1":
            _apagar_estado(cur, chave)
            return _dar_baixa(cur, estado, operador, canal) + RODAPE
        if n == "2":
            _apagar_estado(cur, chave)
            return "Cancelado. Nada foi alterado." + RODAPE
        return "Responda *1* para dar baixa ou *2* para cancelar."

    _apagar_estado(cur, chave)
    return MENU


def _iniciar_fluxo(cur, chave, opcao):
    fluxos = {"1": ("compra", "*Registrar compra*\nQuem está comprando? Digite o nome."),
              "2": ("consulta", "*Consultar conta*\nDe quem você quer ver a conta? Digite o nome."),
              "4": ("pagamento", "*Registrar pagamento*\nQuem pagou? Digite o nome.")}
    fluxo, pergunta = fluxos[opcao]
    _salvar_estado(cur, chave, {"fluxo": fluxo, "etapa": "quem", "itens": []})
    return pergunta + "\n(Digite *menu* para cancelar.)"


def tratar_mensagem(conn, chave, operador, texto, canal="WhatsApp"):
    """Processa uma mensagem de um numero autorizado e devolve a resposta."""
    cur = conn.cursor()
    cur.execute("SELECT id, nome FROM pessoas")
    pessoas = [dict(r) for r in cur.fetchall()]
    cur.execute("SELECT id, nome, preco FROM produtos WHERE ativo")
    produtos = [dict(r) for r in cur.fetchall()]
    idx_produtos = [(p, tokens_produto(p["nome"])) for p in produtos]

    estado = _carregar_estado(cur, chave)
    n = normalizar(texto)

    if n in MENU_PALAVRAS or n.startswith("ajuda"):
        if estado:
            _apagar_estado(cur, chave)
        return MENU
    if estado and estado.get("fluxo"):
        return _passo_fluxo(conn, cur, chave, operador, estado, n, pessoas, idx_produtos, canal)

    if estado:  # conversa iniciada pelo atalho de frase
        resultado = continuar(estado, texto, pessoas)
        if resultado == "cancelar":
            _apagar_estado(cur, chave)
            return "Cancelado. Nada foi registrado." + RODAPE
        if resultado == "confirmado":
            _apagar_estado(cur, chave)
            return _registrar(conn, estado, operador, canal) + RODAPE
        if resultado == "ok":
            if estado.get("escolhido"):
                _apagar_estado(cur, chave)
                return _texto_conta(cur, estado["escolhido"]) + RODAPE
            pergunta = proxima_pergunta(estado)
            if pergunta is None:
                estado["etapa"] = "confirmar"
                pergunta = texto_confirmacao(estado)
            _salvar_estado(cur, chave, estado)
            return pergunta
        _apagar_estado(cur, chave)  # nao era resposta: trata como mensagem nova

    if n in ("1", "2", "4"):
        return _iniciar_fluxo(cur, chave, n)
    if n == "3":
        return _texto_ranking(cur) + RODAPE
    if n in LISTA_PALAVRAS:
        return _texto_lista(cur) + RODAPE
    if n in CANCELAR:
        return "Não há nada para cancelar." + RODAPE
    if n in CONFIRMAR:
        return "Não há nada esperando confirmação." + RODAPE

    # atalho: frase completa, ex. "Kevin 2 coxinhas" ou "quanto o Kevin deve"
    palavras = n.split()
    if any(p in PALAVRAS_CONSULTA for p in palavras) and not any(p.isdigit() for p in palavras):
        restantes = [p for p in palavras if p not in RUIDO_CONSULTA]
        if not restantes or "quem" in palavras:
            return _texto_ranking(cur) + RODAPE
        return _consultar_pessoa(cur, chave, restantes, pessoas)

    novo, nao_entendidos = montar_estado(texto, pessoas, produtos)
    if nao_entendidos:
        lista = ", ".join(f"*{x}*" for x in nao_entendidos)
        return (f"Não achei no cardápio: {lista}. Nada foi registrado.\n"
                "Digite *lista* para ver os produtos ou *menu* para o passo a passo.")
    if not novo["itens"]:
        idx = [(p, tokens_pessoa(p["nome"])) for p in pessoas]
        tipo, val, _ = resolver_pessoa(palavras, idx) if palavras else ("nenhum", None, 0)
        if tipo == "unico":
            return _texto_conta(cur, val) + RODAPE
        return "Não entendi essa mensagem.\n\n" + MENU

    pergunta = proxima_pergunta(novo)
    if pergunta is None:
        novo["etapa"] = "confirmar"
        pergunta = texto_confirmacao(novo)
    _salvar_estado(cur, chave, novo)
    return pergunta


def _consultar_pessoa(cur, chave, palavras, pessoas):
    idx = [(p, tokens_pessoa(p["nome"])) for p in pessoas]
    tipo, val, total = resolver_pessoa(palavras, idx)
    if tipo == "unico":
        return _texto_conta(cur, val) + RODAPE
    if tipo == "ambiguo":
        estado = {"etapa": "escolher_consulta",
                  "candidatos": [{"id": p["id"], "nome": p["nome"]} for p in val]}
        _salvar_estado(cur, chave, estado)
        return _opcoes_texto(f"De qual pessoa você quer saber, *{' '.join(palavras)}*?",
                             estado["candidatos"], total, lambda p: p["nome"])
    return f"Não achei ninguém chamado *{' '.join(palavras)}*." + RODAPE


# ---------------------------------------------------------- WhatsApp (Meta)

def chave_telefone(numero):
    d = re.sub(r"\D", "", numero or "")
    if d.startswith("55") and len(d) >= 12:
        d = d[2:]
    return d[:2] + d[-8:] if len(d) >= 10 else d


def numeros_permitidos():
    mapa = {}
    for item in os.environ.get("WHATSAPP_NUMEROS", "").split(","):
        item = item.strip()
        if not item:
            continue
        numero, _, nome = item.partition(":")
        mapa[chave_telefone(numero)] = nome.strip() or numero.strip()
    return mapa


def assinatura_valida(corpo, cabecalho):
    segredo = os.environ.get("WHATSAPP_APP_SECRET")
    if not segredo:
        return os.environ.get("WHATSAPP_SEM_ASSINATURA") == "1"
    esperado = "sha256=" + hmac.new(segredo.encode(), corpo, hashlib.sha256).hexdigest()
    return hmac.compare_digest(esperado, cabecalho or "")


def enviar_texto(para, texto):
    token = os.environ.get("WHATSAPP_TOKEN")
    phone_id = os.environ.get("WHATSAPP_PHONE_ID")
    if not token or not phone_id:
        log.warning("WHATSAPP_TOKEN/WHATSAPP_PHONE_ID nao configurados; resposta nao enviada")
        return False
    corpo = json.dumps({"messaging_product": "whatsapp", "to": para, "type": "text",
                        "text": {"body": texto[:4000]}}).encode()
    req = urllib.request.Request(
        f"https://graph.facebook.com/{VERSAO_API}/{phone_id}/messages", data=corpo, method="POST",
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=10):
            return True
    except urllib.error.HTTPError as e:
        log.error("Meta recusou o envio: %s %s", e.code, e.read()[:300])
    except Exception:
        log.exception("Falha ao enviar resposta no WhatsApp")
    return False


def extrair_mensagens(payload):
    """Lista de (id, telefone, tipo, texto) das mensagens recebidas no webhook."""
    saida = []
    for entrada in payload.get("entry", []):
        for mudanca in entrada.get("changes", []):
            for msg in mudanca.get("value", {}).get("messages", []) or []:
                texto = (msg.get("text") or {}).get("body", "")
                saida.append((msg.get("id"), msg.get("from"), msg.get("type"), texto))
    return saida


def processar_webhook(payload, abrir_conexao):
    """Trata cada mensagem recebida. Nunca levanta erro (a Meta reenvia se o webhook falhar)."""
    permitidos = numeros_permitidos()
    for msg_id, telefone, tipo, texto in extrair_mensagens(payload):
        if not msg_id or not telefone:
            continue
        chave = chave_telefone(telefone)
        operador = permitidos.get(chave)
        conn = abrir_conexao()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    """INSERT INTO whatsapp_mensagens (id, telefone, nome, texto)
                       VALUES (%s, %s, %s, %s) ON CONFLICT (id) DO NOTHING RETURNING id""",
                    (msg_id, telefone, operador, texto),
                )
                if cur.fetchone() is None:
                    conn.commit()
                    continue  # mensagem repetida pela Meta: ja tratada
            conn.commit()

            if operador is None:
                resposta = "Este número não está autorizado a registrar contas. Peça para ser cadastrado."
            elif tipo != "text":
                resposta = "Por enquanto só entendo mensagens de texto."
            else:
                try:
                    resposta = tratar_mensagem(conn, chave, operador, texto)
                    conn.commit()
                except Exception:
                    conn.rollback()
                    log.exception("Erro ao tratar mensagem do WhatsApp")
                    resposta = "Não consegui processar essa mensagem. Nada foi registrado; tente de novo."

            with conn.cursor() as cur:
                cur.execute("UPDATE whatsapp_mensagens SET resposta = %s WHERE id = %s", (resposta, msg_id))
            conn.commit()
            enviar_texto(telefone, resposta)
        except Exception:
            conn.rollback()
            log.exception("Falha no webhook do WhatsApp")
        finally:
            conn.close()
