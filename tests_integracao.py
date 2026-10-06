"""Teste de ponta a ponta do app, do bot e dos avisos.
Roda num SCHEMA TEMPORARIO do Postgres (criado e apagado por este script), entao nao toca nos
dados reais. Nao envia nada ao Telegram de verdade.
Rode com: python tests_integracao.py     (precisa do DATABASE_URL no .env)
"""
import json
import os
import sys
import uuid
from datetime import timedelta

RAIZ = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, RAIZ)
os.chdir(RAIZ)

from dotenv import load_dotenv  # noqa: E402

load_dotenv()
os.environ.update(TELEGRAM_USUARIOS="111:Yan,222:Ana", TELEGRAM_WEBHOOK_SECRET="seg-teste",
                  TELEGRAM_TOKEN="123456:TOKEN-FALSO", COBRANCA_PIX="chave@pix.com", ESTOQUE_MINIMO="5",
                  RESUMO_HORA="20")
os.environ.pop("RENDER_EXTERNAL_URL", None)

import psycopg2  # noqa: E402
import psycopg2.extras  # noqa: E402

import db  # noqa: E402

ESQUEMA = "teste_" + uuid.uuid4().hex[:8]
URL = db.DATABASE_URL
if not URL:
    sys.exit("DATABASE_URL nao configurada (.env)")

_adm = psycopg2.connect(URL)
_adm.autocommit = True
_adm.cursor().execute(f"CREATE SCHEMA {ESQUEMA}")


def _conexao_de_teste():
    c = psycopg2.connect(URL, cursor_factory=psycopg2.extras.RealDictCursor)
    with c.cursor() as cur:
        cur.execute(f"SET search_path TO {ESQUEMA}")
    c.commit()
    return c


db.get_connection = _conexao_de_teste  # antes de importar o app: ele copia o nome

import notificacoes  # noqa: E402
import servicos  # noqa: E402
import telegram_bot  # noqa: E402
import whatsapp_bot as bot  # noqa: E402
from app import app  # noqa: E402

# ------------------------------------------------------------ Telegram falso
CHAMADAS = []
_proximo_id = [1000]


def _chamar_falso(metodo, corpo=None):
    CHAMADAS.append((metodo, corpo or {}))
    if metodo == "sendMessage":
        _proximo_id[0] += 1
        return {"ok": True, "result": {"message_id": _proximo_id[0]}}
    if metodo == "getMe":
        return {"ok": True, "result": {"username": "palacios_bot", "id": 123456}}
    return {"ok": True, "result": True}


DOCUMENTOS = []
telegram_bot.chamar = _chamar_falso
telegram_bot.enviar_documento = lambda chat, conteudo, nome, legenda="": DOCUMENTOS.append((chat, nome, len(conteudo))) or True

FALHAS, TOTAL = [], [0]


def confere(condicao, descricao):
    TOTAL[0] += 1
    if not condicao:
        FALHAS.append(descricao)
        print("  FALHOU:", descricao)


def q(sql, params=()):
    c = _conexao_de_teste()
    with c.cursor() as cur:
        cur.execute(sql, params or None)
        linhas = cur.fetchall() if cur.description else []
    c.commit()
    c.close()
    return linhas


def ex(sql, params=()):
    q(sql, params)


def estoque(nome):
    return q("SELECT estoque FROM produtos WHERE nome = %s", (nome,))[0]["estoque"]


def pessoa_id(nome):
    return q("SELECT id FROM pessoas WHERE nome = %s", (nome,))[0]["id"]


def falar(operador, texto, canal="Telegram", chave="t:1"):
    c = _conexao_de_teste()
    r = bot.tratar_mensagem(c, chave, operador, texto, canal=canal)
    c.commit()
    c.close()
    return r


def conversa(passos, operador="Yan", chave="t:1"):
    saida = None
    for p in passos:
        saida = falar(operador, p, chave=chave)
    return saida


try:
    # ------------------------------------------------------------ dados
    ex("""INSERT INTO produtos (numero_item, nome, preco, categoria, estoque, custo) VALUES
          (1, 'Coxinha', 8, 'restaurante', 20, 0), (2, 'Coca Cola lata 350ml', 7, 'mercadinho', 10, 0),
          (3, 'Coca cola Pet 200ml', 4, 'mercadinho', 10, 0), (4, 'Esfiha', 8, 'restaurante', 6, 0)""")
    ex("INSERT INTO pessoas (nome) VALUES ('KEVIN'), ('KARINA PREPARAÇÃO'), ('DAVI')")

    print("1. pagamento parcial (servicos)")
    kev = pessoa_id("KEVIN")
    cox = q("SELECT id FROM produtos WHERE nome = 'Coxinha'")[0]["id"]
    for dia, valor in [("2026-09-01", 16), ("2026-09-02", 8), ("2026-09-03", 7)]:
        ex("""INSERT INTO pedidos (data, pessoa_id, produto_id, quantidade, valor_unitario, valor_total)
              VALUES (%s, %s, %s, 1, %s, %s)""", (dia, kev, cox, valor, valor))
    c = _conexao_de_teste()
    cur = c.cursor()
    r = servicos.aplicar_pagamento(cur, kev, 10, "Pix", "2026-10-05", "t")
    confere(r["restante"] == 21 and r["quitados"] == 0, "10 de 31: restam 21, nenhum quitado")
    r = servicos.aplicar_pagamento(cur, kev, 8, "Pix", "2026-10-05", "t")
    confere(r["restante"] == 13 and r["quitados"] == 1, "mais 8: primeiro lancamento quitado")
    for ruim in (14, 0, -3):
        try:
            servicos.aplicar_pagamento(cur, kev, ruim, "Pix", "2026-10-05", "t")
            confere(False, f"valor {ruim} deveria ser recusado")
        except ValueError:
            confere(True, "")
    cur.execute("SELECT valor_total, valor_pago, pago FROM pedidos WHERE pessoa_id = %s ORDER BY data", (kev,))
    linhas = cur.fetchall()
    confere([l["valor_pago"] for l in linhas] == [16, 2, 0] and [l["pago"] for l in linhas] == [True, False, False],
            "abate do mais antigo para o mais novo")
    r = servicos.aplicar_pagamento(cur, kev, None, "Dinheiro", "2026-10-05", "t")
    confere(r["restante"] == 0 and r["quitados"] == 2, "quitar tudo")
    try:
        servicos.aplicar_pagamento(cur, kev, None, "Pix", "2026-10-05", "t")
        confere(False, "sem divida deveria recusar")
    except ValueError:
        confere(True, "")
    cur.execute("SELECT COUNT(*) n, SUM(valor) t FROM pagamentos WHERE pessoa_id = %s", (kev,))
    g = cur.fetchone()
    confere(g["n"] == 3 and abs(g["t"] - 31) < 0.001, "tres pagamentos somando 31")
    c.commit()
    c.close()
    confere(bot.ler_valor("20") == 20 and bot.ler_valor("20,50") == 20.5 and bot.ler_valor("R$ 1.020,50") == 1020.5
            and bot.ler_valor("1.020") == 1020 and bot.ler_valor("abc") is None and bot.ler_valor("12.5") == 12.5,
            "ler_valor")

    print("2. bot: menu, compra, desfazer")
    menu = falar("Yan", "oi")
    confere(all(f"{i})" in menu for i in range(1, 8)), "menu com 7 opcoes")
    r = conversa(["1", "davi", "coxinha", "3", "2", "1"])
    confere("Registrado" in r and "R$ 24,00" in r, "compra registrada: " + r[:60])
    ped = q("SELECT criado_por, lote, observacoes FROM pedidos WHERE pessoa_id = %s", (pessoa_id("DAVI"),))
    confere(len(ped) == 1 and ped[0]["criado_por"] == "Telegram:Yan" and ped[0]["lote"], "pedido com lote e autor")
    confere(estoque("Coxinha") == 17, "estoque desceu")
    r = conversa(["6"])
    confere("Último lançamento de *DAVI*" in r and "1) Sim, desfazer" in r, "desfazer mostra o lancamento")
    r = conversa(["1"])
    confere("Desfeito" in r and estoque("Coxinha") == 20, "desfez e devolveu estoque")
    confere(q("SELECT COUNT(*) n FROM pedidos WHERE pessoa_id = %s", (pessoa_id("DAVI"),))[0]["n"] == 0, "pedido removido")
    r = falar("Yan", "6")
    confere("Não achei nenhum lançamento seu" in r, "nada mais para desfazer")
    r = falar("Ana", "6", chave="t:2")
    confere("Não achei nenhum lançamento seu" in r, "desfazer so alcanca o proprio lancamento")

    print("3. bot: pagamento parcial")
    conversa(["1", "davi", "coxinha 3", "2", "1"])
    r = conversa(["4", "davi"])
    confere("1) Tudo (R$ 24,00)" in r and "2) Só uma parte" in r, "pergunta tudo ou parte")
    r = conversa(["2"])
    confere("Quanto a pessoa pagou" in r, "pede o valor")
    r = falar("Yan", "abc")
    confere("Não entendi o valor" in r, "valor invalido")
    r = falar("Yan", "999")
    confere("passa do que a pessoa deve" in r, "valor acima da divida")
    r = falar("Yan", "10")
    confere("Como foi o pagamento" in r, "pergunta a forma")
    r = falar("Yan", "1")
    confere("R$ 10,00" in r and "ainda deve R$ 14,00" in r, "confirmacao do parcial: " + r)
    r = falar("Yan", "1")
    confere("Pagamento registrado" in r and "Ainda deve R$ 14,00" in r, "parcial registrado: " + r)
    p = q("SELECT valor_pago, pago FROM pedidos WHERE pessoa_id = %s", (pessoa_id("DAVI"),))[0]
    confere(p["valor_pago"] == 10 and not p["pago"], "lancamento segue em aberto com 10 pagos")
    r = falar("Yan", "2")
    confere("Davi" not in r or "deve" in r, "consulta")
    r = conversa(["2", "davi"])
    confere("R$ 14,00" in r, "conta mostra so o que falta: " + r)
    r = conversa(["7", "davi"])
    confere("Oi, Davi!" in r and "R$ 14,00" in r and "Pix: chave@pix.com" in r, "mensagem de cobranca: " + r)
    r = conversa(["6", "1"])
    confere("já recebeu pagamento" in r, "nao desfaz lancamento com pagamento: " + r)
    r = conversa(["3"])
    confere("1. DAVI: R$ 14,00" in r and "1)" not in r, "ranking sem virar botao: " + r)
    r = conversa(["4", "davi", "1", "3", "1"])
    confere("A conta está quitada" in r, "quita o resto: " + r)
    r = conversa(["7", "davi"])
    confere("não deve nada" in r, "cobranca sem divida")
    r = falar("Yan", "resumo")
    confere("Resumo de hoje" in r and "Recebido hoje" in r, "resumo do dia: " + r)
    confere(falar("Yan", "5").startswith("*Resumo de hoje"), "opcao 5")

    print("4. web: login, venda, pagamento, edicao, custos")
    cli = app.test_client()
    confere(cli.get("/").status_code == 200, "painel abre sem login")
    confere(cli.get("/login").status_code == 404, "nao existe tela de login")
    confere(cli.get("/saude").status_code == 200, "/saude")
    confere(cli.get("/offline").status_code == 200, "/offline")
    confere(cli.get("/sw.js").status_code == 200 and "javascript" in cli.get("/sw.js").content_type, "service worker")
    confere(cli.get("/static/manifest.json").status_code == 200, "manifest")
    confere(cli.post("/telegram/webhook", json={}).status_code == 403, "webhook exige segredo")
    for rota in ["/mercadinho", "/restaurante", "/estoque", "/contas", "/cardapio", "/custos", "/financeiro",
                 "/historico", "/bot", "/exportar", f"/contas/{kev}"]:
        confere(cli.get(rota).status_code == 200, f"GET {rota}")

    itens = json.dumps([{"produto_id": cox, "quantidade": 2}])
    r = cli.post("/restaurante", data={"pessoa": "Maria Nova", "itens": itens}, follow_redirects=True)
    confere(b"Registrado para Maria Nova" in r.data, "venda pelo app")
    mar = pessoa_id("Maria Nova")
    ped = q("SELECT id, criado_por, lote, valor_total FROM pedidos WHERE pessoa_id = %s", (mar,))[0]
    confere(ped["criado_por"] == "App" and ped["lote"], "autor da venda pelo app")
    confere(estoque("Coxinha") == 15, "estoque da venda do app")
    hist = q("SELECT quem, acao FROM historico WHERE acao = 'Registrou venda' AND quem = 'App'")
    confere(len(hist) == 1, "historico da venda do app")

    r = cli.get(f"/pedidos/{ped['id']}/editar")
    confere(r.status_code == 200 and b"Editar lan" in r.data, "tela de editar")
    esf = q("SELECT id FROM produtos WHERE nome = 'Esfiha'")[0]["id"]
    r = cli.post(f"/pedidos/{ped['id']}/editar",
                 data={"pessoa_id": pessoa_id("DAVI"), "produto_id": esf, "quantidade": "3", "data": "2026-10-01",
                       "observacoes": "corrigido"}, follow_redirects=True)
    confere(b"Lan" in r.data and r.status_code == 200, "editar salvou")
    novo = q("SELECT pessoa_id, produto_id, quantidade, valor_total, data, observacoes FROM pedidos WHERE id = %s",
             (ped["id"],))[0]
    confere(novo["pessoa_id"] == pessoa_id("DAVI") and novo["produto_id"] == esf and novo["quantidade"] == 3
            and novo["valor_total"] == 24 and novo["observacoes"] == "corrigido", "editar mudou pessoa/produto/qtd/valor")
    confere(estoque("Coxinha") == 17 and estoque("Esfiha") == 3, "editar ajustou os dois estoques")

    davi = pessoa_id("DAVI")
    r = cli.get(f"/contas/{davi}")
    confere(b"Mensagem de cobran" in r.data and b"Pagou s" in r.data, "conta mostra cobranca e pagamento parcial")
    r = cli.post(f"/contas/{davi}/pagar-parte", data={"valor": "10,50", "forma_pagamento": "Pix"}, follow_redirects=True)
    confere(b"Pagamento de R$ 10,50 registrado" in r.data, "pagar parte pelo app: " + r.get_data(as_text=True)[:0])
    r = cli.post(f"/contas/{davi}/pagar-parte", data={"valor": "9999"}, follow_redirects=True)
    confere("maior que a dívida".encode() in r.data, "pagar parte acima da divida")
    r = cli.post(f"/pedidos/{ped['id']}/editar", data={"pessoa_id": davi, "produto_id": cox, "quantidade": "9",
                                                       "data": "2026-10-01"}, follow_redirects=True)
    confere(estoque("Esfiha") == 3, "lancamento com pagamento nao muda quantidade/produto")
    r = cli.post(f"/contas/{davi}/pagar-tudo", data={"forma_pagamento": "Pix"}, follow_redirects=True)
    confere(b"quitada" in r.data, "pagar tudo")
    confere(q("SELECT COUNT(*) n FROM pedidos WHERE pessoa_id = %s AND NOT pago", (davi,))[0]["n"] == 0, "nada em aberto")

    r = cli.post("/mercadinho", data={"pessoa": "KEVIN", "itens": json.dumps(
        [{"produto_id": q("SELECT id FROM produtos WHERE nome = 'Coca Cola lata 350ml'")[0]["id"], "quantidade": 1}]),
        "pago": "on", "forma_pagamento": "Dinheiro"}, follow_redirects=True)
    confere(q("SELECT valor_pago, pago FROM pedidos WHERE pessoa_id = %s ORDER BY id DESC LIMIT 1", (kev,))[0]["pago"],
            "venda ja paga no app")

    # custos em massa, com retroativo
    cx = q("SELECT COUNT(*) n FROM pedidos WHERE custo_unitario = 0 AND produto_id = %s", (cox,))[0]["n"]
    r = cli.post("/custos", data={f"custo_{cox}": "3,20", "retroativo": "on"}, follow_redirects=True)
    confere(b"Custos salvos" in r.data, "custos salvos")
    confere(q("SELECT custo FROM produtos WHERE id = %s", (cox,))[0]["custo"] == 3.2, "custo gravado")
    confere(q("SELECT COUNT(*) n FROM pedidos WHERE produto_id = %s AND custo_unitario = 3.2", (cox,))[0]["n"] == cx,
            "custo aplicado as vendas antigas sem custo")
    r = cli.post("/custos", data={f"custo_{cox}": "4", "retroativo": "on"}, follow_redirects=True)
    confere(q("SELECT COUNT(*) n FROM pedidos WHERE produto_id = %s AND custo_unitario = 3.2", (cox,))[0]["n"] == cx,
            "vendas que ja tinham custo nao mudam")

    r = cli.get("/exportar")
    confere(r.status_code == 200 and r.data[:2] == b"PK", "excel valido")
    r = cli.get("/historico")
    confere(b"Editou lan" in r.data and b"Registrou pagamento" in r.data and b"Atualizou custos" in r.data, "historico completo")
    r = cli.post(f"/pedidos/{ped['id']}/excluir", follow_redirects=True)
    confere(b"exclu" in r.data, "excluir")

    print("5. telegram: botoes, grupos, duplicados")
    CHAMADAS.clear()
    _uid = [5000]

    def upd(texto=None, uid=111, tipo="private", extra=None, cb=None):
        _uid[0] += 1
        if cb is not None:
            return {"update_id": _uid[0], "callback_query": {"id": f"cb{_uid[0]}", "from": {"id": uid}, "data": cb[0],
                                                             "message": {"message_id": cb[1], "chat": {"id": uid, "type": tipo}}}}
        msg = {"message_id": _uid[0], "from": {"id": uid, "first_name": "X"}, "chat": {"id": uid if tipo == "private" else -900, "type": tipo}}
        if texto is not None:
            msg["text"] = texto
        if extra:
            msg.update(extra)
        return {"update_id": _uid[0], "message": msg}

    def enviar(update):
        antes = len(CHAMADAS)
        telegram_bot.processar_update(update, db.get_connection)
        return CHAMADAS[antes:]

    def envios(chamadas):
        return [c[1] for c in chamadas if c[0] == "sendMessage"]

    r = envios(enviar(upd("/start")))
    confere(len(r) == 1 and "reply_markup" in r[0], "menu vem com botoes")
    botoes = [b for linha in r[0]["reply_markup"]["inline_keyboard"] for b in linha]
    confere(len(botoes) == 7 and botoes[0]["callback_data"] == "111|1", "7 botoes, dono no callback")
    msg_menu = _proximo_id[0]

    cham = enviar(upd(cb=("111|1", msg_menu)))
    metodos = [c[0] for c in cham]
    confere(metodos[0] == "answerCallbackQuery" and "editMessageReplyMarkup" in metodos, "toque confirma e limpa o botao")
    r = envios(cham)
    confere("Quem está comprando" in r[0]["text"], "toque iniciou o fluxo")
    cham = enviar(upd(cb=("999|1", 1)))
    confere(cham[0][1].get("text") == "Esse botão é de outra pessoa." and not envios(cham), "botao de outra pessoa recusado")
    enviar(upd("davi"))
    r = envios(enviar(upd("coxinha")))
    confere("Quantas unidades" in r[0]["text"], "pergunta quantidade")
    teclado = [b["text"] for linha in r[0]["reply_markup"]["inline_keyboard"] for b in linha]
    confere("1" in teclado and "12" in teclado and "Menu" in teclado, "atalhos de quantidade e menu")
    r = envios(enviar(upd("2")))
    confere("Adicionar outro item" in r[0]["text"], "item adicionado")
    cham = enviar(upd("2"))
    confere(any(c[0] == "editMessageReplyMarkup" for c in cham), "botao da resposta anterior removido ao digitar")
    r = envios(cham)
    confere("Confirma?" in r[0]["text"], "confirmacao")
    r = envios(enviar(upd("1")))
    confere("Registrado" in r[0]["text"], "registrou pelo telegram")
    confere(q("SELECT COUNT(*) n FROM pedidos WHERE criado_por = 'Telegram:Yan' AND observacoes = 'via Telegram (Yan)'")[0]["n"] >= 1,
            "pedido do telegram com observacao")

    dup = upd("menu")
    enviar(dup)
    confere(enviar(dup) == [], "update repetido ignorado")
    r = envios(enviar(upd("oi", uid=999)))
    confere("999" in r[0]["text"] and "reply_markup" not in r[0], "desconhecido recebe o proprio ID")
    r = enviar(upd(extra={"sticker": {"file_id": "x"}}))
    confere("só entendo mensagens de texto" in envios(r)[0]["text"], "figurinha")

    # grupos
    confere(not envios(enviar(upd("bom dia pessoal", tipo="supergroup"))), "grupo: conversa comum ignorada")
    r = envios(enviar(upd("/menu", tipo="supergroup")))
    confere(len(r) == 1 and r[0]["chat_id"] == -900 and r[0].get("reply_to_message_id"), "grupo: comando responde ao autor")
    confere(not envios(enviar(upd("/menu@outro_bot", tipo="supergroup"))), "grupo: comando de outro bot ignorado")
    r = envios(enviar(upd("@palacios_bot quanto o davi deve", tipo="supergroup")))
    confere(len(r) == 1 and "DAVI" in r[0]["text"], "grupo: mencao funciona")
    r = envios(enviar(upd("1", tipo="supergroup", extra={"reply_to_message": {"from": {"id": 123456, "is_bot": True}}})))
    confere(len(r) == 1, "grupo: resposta a mensagem do bot funciona")
    confere(enviar(upd("1", tipo="supergroup", extra={"reply_to_message": {"from": {"id": 5, "is_bot": False}}})) == [],
            "grupo: resposta a outra pessoa ignorada")
    r = envios(enviar(upd("1", tipo="supergroup", extra={"reply_to_message": {"from": {"id": 123456, "is_bot": True}}})))
    confere(r and "Responda a esta mensagem" in r[0]["text"], "grupo: lembra de responder a mensagem")
    falar("Yan", "menu", chave="tg:-900:111")

    print("6. avisos")
    ex("UPDATE produtos SET estoque = 2 WHERE nome = 'Esfiha'")
    CHAMADAS.clear()
    c = _conexao_de_teste()
    notificacoes.checar_estoque(c)
    notificacoes.checar_estoque(c)
    c.close()
    r = envios(CHAMADAS)
    confere(len(r) == 1 and "Estoque baixo" in r[0]["text"] and "Esfiha" in r[0]["text"] and r[0]["chat_id"] == 111,
            "alerta de estoque sai uma vez so, para o primeiro usuario")
    ex("UPDATE produtos SET estoque = 30 WHERE nome = 'Esfiha'")
    c = _conexao_de_teste()
    notificacoes.checar_estoque(c)
    CHAMADAS.clear()
    ex("UPDATE produtos SET estoque = 1 WHERE nome = 'Esfiha'")
    notificacoes.checar_estoque(c)
    c.close()
    confere(len(envios(CHAMADAS)) == 1, "depois de repor, avisa de novo quando cair")

    class Relogio:
        def __init__(self, hora):
            self.h = hora

    original = servicos.agora
    servicos.agora = lambda: original().replace(hour=19, minute=0)
    CHAMADAS.clear()
    notificacoes.rodar_tarefas(db.get_connection)
    confere(not any("Resumo" in m.get("text", "") for m in envios(CHAMADAS)) and not DOCUMENTOS, "antes das 20h nao manda resumo")
    servicos.agora = lambda: original().replace(hour=21, minute=0)
    CHAMADAS.clear()
    notificacoes.rodar_tarefas(db.get_connection)
    notificacoes.rodar_tarefas(db.get_connection)
    resumos = [m for m in envios(CHAMADAS) if "Resumo de hoje" in m["text"]]
    confere(len(resumos) == 1, "resumo do dia sai uma vez")
    confere(len(DOCUMENTOS) == 1 and DOCUMENTOS[0][1].endswith(".xlsx") and DOCUMENTOS[0][2] > 1000, "backup semanal sai uma vez")
    ex("UPDATE avisos SET criado_em = criado_em - INTERVAL '8 days' WHERE chave LIKE 'backup:%'")
    servicos.agora = lambda: original().replace(hour=21) + timedelta(days=8)
    notificacoes.rodar_tarefas(db.get_connection)
    confere(len(DOCUMENTOS) == 2, "oito dias depois manda outro backup")
    servicos.agora = original

    cli2 = app.test_client()
    confere(b"Ligados" in cli2.get("/bot").data, "pagina do bot mostra avisos ligados")
    confere(cli2.get("/saude").data == b"ok", "/saude responde ok")
    confere(b"Hoje" not in b"" and q("SELECT 1 FROM avisos WHERE chave = 'ping'") != [], "ping registrado")
    r = cli2.post("/bot/backup", follow_redirects=True)
    confere(b"Backup enviado" in r.data, "botao de backup")
    r = cli2.post("/bot/avisos/teste", follow_redirects=True)
    confere(b"Resumo enviado" in r.data, "botao de resumo")

    print("7. compra de estoque, lucro por item e contagem")
    import importar_estoque
    ARQ = os.path.join(os.path.expanduser("~"), "Downloads", "palacios_estoque_claude.json")
    if os.path.exists(ARQ):
        dados = json.load(open(ARQ, encoding="utf-8"))
        ex("""INSERT INTO produtos (numero_item, nome, preco, categoria, estoque, custo) VALUES
              (8, 'Coca cola Pet 200ml', 4, 'mercadinho', 0, 0), (9, 'Coca Cola lata 350ml', 7, 'mercadinho', 3, 0),
              (13, 'Guaraná Antarctica Pet 200ml', 4, 'mercadinho', 0, 0)""")
        antes = q("SELECT COUNT(*) n FROM produtos")[0]["n"]
        importar_estoque.importar(ARQ, dry_run=True)
        confere(q("SELECT COUNT(*) n FROM produtos")[0]["n"] == antes, "simulacao nao grava")
        importar_estoque.importar(ARQ)
        n_json = len(dados["produtos"])
        confere(q("SELECT COUNT(*) n FROM produtos")[0]["n"] == antes + n_json - 3, "so cria o que nao existe (3 ja existiam)")
        ent = q("SELECT COALESCE(SUM(quantidade),0) u, COALESCE(SUM(custo_total),0) c FROM entradas_estoque WHERE referencia IS NOT NULL")[0]
        confere(ent["u"] == 154 and abs(ent["c"] - 299.63) < 0.011, f"154 unidades e R$ 299,63 de custo ({ent})")
        coca = q("SELECT estoque, custo, preco, ean, sku FROM produtos WHERE numero_item = 9")[0]
        confere(coca["estoque"] == 15 and abs(coca["custo"] - 3.8) < 1e-9 and coca["preco"] == 7 and coca["ean"] == "07894900010015",
                f"coca existente: soma estoque, novo custo e mantem o preco ({coca})")
        confere(q("SELECT COUNT(*) n FROM produtos WHERE nome ILIKE '%coca%zero%'")[0]["n"] == 1, "coca zero criada")
        importar_estoque.importar(ARQ)
        confere(q("SELECT SUM(estoque) s FROM produtos WHERE ean IS NOT NULL")[0]["s"] == 157, "rodar de novo nao soma de novo")
        pop = q("SELECT id, estoque, estoque_estimado, obs_estoque FROM produtos WHERE sku = 'PIR-POPKISS-MEL-500'")[0]
        confere(pop["estoque"] == 50 and pop["estoque_estimado"] and "estimado" in pop["obs_estoque"], "pop kiss marcado como estimado")

        cli3 = app.test_client()
        r = cli3.get("/")
        confere(r.status_code == 200 and "Lucro possível".encode() in r.data and b"info-estoque" in r.data, "painel mostra o resumo do estoque")
        c = _conexao_de_teste()
        info = servicos.metricas_estoque(c.cursor())
        c.close()
        confere(info["potencial"] > info["investido"] > 0 and len(info["top"]) == 5 and info["unidades"] > 154,
                "metricas do estoque")
        confere(abs(info["lucro"] - (info["potencial"] - info["investido"])) < 1e-6, "lucro = valor de venda - investido")
        for rota in ("/estoque", "/cardapio", "/restaurante", "/mercadinho", "/custos"):
            r = cli3.get(rota)
            confere(r.status_code == 200 and (b"info-btn" in r.data or rota == "/custos"), f"{rota} abre")
        confere(b"Quantidade estimada" in cli3.get("/estoque").data, "estoque mostra contagem para o estimado")
        confere(b'step="any"' in cli3.get("/custos").data, "campo de custo aceita 4 casas")
        r = cli3.post(f"/estoque/{pop['id']}/contagem", data={"contagem": "48"}, follow_redirects=True)
        confere(b"Contagem confirmada" in r.data, "contagem enviada")
        pop2 = q("SELECT estoque, estoque_estimado FROM produtos WHERE id = %s", (pop["id"],))[0]
        confere(pop2["estoque"] == 48 and not pop2["estoque_estimado"], "contagem ajusta e tira o aviso de estimado")
        confere(q("SELECT quantidade FROM entradas_estoque WHERE produto_id = %s ORDER BY id DESC LIMIT 1", (pop["id"],))[0]["quantidade"] == -2,
                "ajuste vira entrada de -2")
        confere(b"-2" in cli3.get("/estoque").data, "entrada negativa aparece sem '+'")
        cli3.post("/custos", data={f"custo_{pop['id']}": "0.1958"}, follow_redirects=True)
        confere(q("SELECT custo FROM produtos WHERE id = %s", (pop["id"],))[0]["custo"] == 0.1958, "custo com 4 casas preservado")
    else:
        print("   (arquivo palacios_estoque_claude.json nao encontrado: parte pulada)")

    print("8. adicionar item novo pelas telas de venda")
    cli4 = app.test_client()
    r = cli4.post("/produtos/novo", data={"categoria": "restaurante", "nome": "  Torta   de Frango ", "preco": "9,50",
                                         "quantidade": "12", "custo": "4,2"}, follow_redirects=True)
    confere(b"adicionado" in r.data and b"Torta de Frango" in r.data, "item novo do restaurante aparece na tela")
    nt = q("SELECT id, nome, preco, custo, categoria, estoque, numero_item FROM produtos WHERE nome = 'Torta de Frango'")[0]
    confere(nt["categoria"] == "restaurante" and nt["preco"] == 9.5 and nt["custo"] == 4.2 and nt["estoque"] == 12
            and nt["numero_item"] is None, f"dados gravados ({nt})")
    confere(q("SELECT quantidade, observacoes FROM entradas_estoque WHERE produto_id = %s", (nt["id"],))[0]["quantidade"] == 12,
            "estoque inicial vira entrada")
    r = cli4.post("/restaurante", data={"pessoa": "Kevin", "itens": json.dumps([{"produto_id": nt["id"], "quantidade": 5}])},
                  follow_redirects=True)
    confere(estoque("Torta de Frango") == 7, "encomenda do restaurante baixa o estoque do item novo")
    r = cli4.post("/produtos/novo", data={"categoria": "mercadinho", "nome": "torta de frango", "preco": "5"}, follow_redirects=True)
    confere("Já existe".encode() in r.data, "nome repetido e recusado")
    r = cli4.post("/produtos/novo", data={"categoria": "mercadinho", "nome": "Suco de Uva 200ml", "preco": "6", "quantidade": ""},
                  follow_redirects=True)
    confere(b"adicionado" in r.data and estoque("Suco de Uva 200ml") == 0, "item do mercadinho sem quantidade nasce com estoque 0")
    for ruim in ({"nome": "", "preco": "5"}, {"nome": "X item", "preco": ""}, {"nome": "Y item", "preco": "abc"},
                 {"nome": "Z item", "preco": "5", "quantidade": "-3"}):
        antes = q("SELECT COUNT(*) n FROM produtos")[0]["n"]
        cli4.post("/produtos/novo", data={"categoria": "mercadinho", **ruim})
        confere(q("SELECT COUNT(*) n FROM produtos")[0]["n"] == antes, f"entrada invalida recusada {ruim}")
    confere(cli4.post("/produtos/novo", data={"categoria": "outro", "nome": "A", "preco": "1"}).status_code == 302
            and q("SELECT COUNT(*) n FROM produtos WHERE nome = 'A'")[0]["n"] == 0, "categoria invalida recusada")
    cli4.post("/cardapio", data={"acao": "desativar", "produto_id": nt["id"]})
    r = cli4.post("/produtos/novo", data={"categoria": "restaurante", "nome": "TORTA DE FRANGO", "preco": "10", "quantidade": "3"},
                  follow_redirects=True)
    confere(b"voltou para o card" in r.data and estoque("Torta de Frango") == 3, "item removido volta ao cardapio em vez de duplicar")
    confere(b"Adicionar item novo" in cli4.get("/mercadinho").data and b"Quantidade produzida" in cli4.get("/restaurante").data,
            "painel de adicionar aparece nas duas telas")

    print("9. fuso e leituras")
    confere(servicos.agora().tzinfo is not None and servicos.hoje() == servicos.agora().date(), "data no fuso do Brasil")

finally:
    _adm.cursor().execute(f"DROP SCHEMA {ESQUEMA} CASCADE")
    _adm.close()

print()
print(f"{TOTAL[0] - len(FALHAS)}/{TOTAL[0]} verificacoes ok")
if FALHAS:
    print("FALHAS:")
    for f in FALHAS:
        print(" -", f)
    sys.exit(1)
