"""Bot do Telegram: o mesmo menu e passo a passo do WhatsApp, pela API gratuita de bots.
Cada opcao numerada da resposta tambem vira um botao tocavel; digitar o numero continua valendo.

Variaveis de ambiente (configuradas no Render, nunca no codigo):
  TELEGRAM_TOKEN           token que o @BotFather entrega ao criar o bot
  TELEGRAM_WEBHOOK_SECRET  frase secreta (so letras, numeros, _ e -) que voce inventa;
                           o Telegram devolve ela em cada mensagem para provar a origem
  TELEGRAM_USUARIOS        quem pode usar: 123456789:Yan,987654321:Maria  (ID numerico:nome)
  TELEGRAM_AVISOS          (opcional) quem recebe alertas de estoque, resumo do dia e backup;
                           IDs separados por virgula. Sem isso, so o primeiro de TELEGRAM_USUARIOS.
"""
import hmac
import html
import json
import logging
import os
import re
import urllib.error
import urllib.request
import uuid

import notificacoes
from whatsapp_bot import tratar_mensagem

log = logging.getLogger("telegram")

OPCAO = re.compile(r"^(\d{1,2})\) (.+)$", re.M)
TIPOS_GRUPO = ("group", "supergroup")
_usuario_bot = {"valor": None}


def usuarios_permitidos():
    mapa = {}
    for item in os.environ.get("TELEGRAM_USUARIOS", "").split(","):
        uid, _, nome = item.strip().partition(":")
        uid = uid.strip()
        if uid.isdigit():
            mapa[uid] = nome.strip() or uid
    return mapa


def assinatura_valida(cabecalho):
    segredo = os.environ.get("TELEGRAM_WEBHOOK_SECRET")
    if not segredo:
        return os.environ.get("TELEGRAM_SEM_ASSINATURA") == "1"
    return hmac.compare_digest(segredo, cabecalho or "")


def para_html(texto):
    """Troca *negrito* (estilo WhatsApp) por <b>, escapando o resto."""
    seguro = html.escape(texto, quote=False)
    return re.sub(r"\*([^*\n]+)\*", r"<b>\1</b>", seguro)


def chamar(metodo, corpo=None):
    """Chama a API do Telegram. Devolve o JSON de resposta, ou None se nao ha token."""
    token = os.environ.get("TELEGRAM_TOKEN")
    if not token:
        return None
    req = urllib.request.Request(
        f"https://api.telegram.org/bot{token}/{metodo}",
        data=json.dumps(corpo or {}).encode(), method="POST",
        headers={"Content-Type": "application/json"},
    )
    return _executar(req)


def _executar(req):
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as e:
        try:
            return json.loads(e.read())
        except Exception:
            return {"ok": False, "description": f"HTTP {e.code}"}
    except Exception as e:
        log.exception("Falha ao chamar o Telegram")
        return {"ok": False, "description": str(e)}


def enviar_texto(chat_id, texto, botoes=None, responder_a=None):
    """Envia a mensagem. Devolve o JSON de 'result' (com message_id) ou None se falhou."""
    corpo = {"chat_id": chat_id, "text": para_html(texto[:3800]),
             "parse_mode": "HTML", "disable_web_page_preview": True}
    if botoes:
        corpo["reply_markup"] = {"inline_keyboard": botoes}
    if responder_a:
        corpo["reply_to_message_id"] = responder_a
        corpo["allow_sending_without_reply"] = True
    resp = chamar("sendMessage", corpo)
    if resp is None:
        log.warning("TELEGRAM_TOKEN nao configurado; resposta nao enviada")
        return None
    if not resp.get("ok"):
        log.error("Telegram recusou o envio: %s", resp.get("description"))
        return None
    return resp.get("result") or {}


def enviar_documento(chat_id, conteudo, nome_arquivo, legenda=""):
    """Envia um arquivo (bytes) no chat. Devolve True se o Telegram aceitou."""
    token = os.environ.get("TELEGRAM_TOKEN")
    if not token:
        return False
    limite = uuid.uuid4().hex

    def campo(nome, valor):
        return (f"--{limite}\r\nContent-Disposition: form-data; name=\"{nome}\"\r\n\r\n{valor}\r\n").encode()

    corpo = campo("chat_id", chat_id) + campo("caption", legenda)
    corpo += (f"--{limite}\r\nContent-Disposition: form-data; name=\"document\"; "
              f"filename=\"{nome_arquivo}\"\r\nContent-Type: application/octet-stream\r\n\r\n").encode()
    corpo += conteudo + f"\r\n--{limite}--\r\n".encode()
    req = urllib.request.Request(
        f"https://api.telegram.org/bot{token}/sendDocument", data=corpo, method="POST",
        headers={"Content-Type": f"multipart/form-data; boundary={limite}"},
    )
    resp = _executar(req)
    if not resp.get("ok"):
        log.error("Telegram recusou o arquivo: %s", resp.get("description"))
    return bool(resp.get("ok"))


def ativar_webhook(url):
    """Diz ao Telegram para entregar as mensagens e os toques nos botoes neste endereco."""
    return chamar("setWebhook", {"url": url, "secret_token": os.environ.get("TELEGRAM_WEBHOOK_SECRET", ""),
                                 "allowed_updates": ["message", "callback_query"]})


def limpar_comando(texto):
    """'/menu@meubot' vira 'menu'; '/start' vira 'start'."""
    return re.sub(r"^/(\w+)(@\w+)?", r"\1", (texto or "").strip())


# ---------------------------------------------------------------- botoes

def teclado(resposta, dono):
    """Monta os botoes a partir da propria resposta: cada linha '1) texto' vira um botao,
    perguntas de quantidade ganham atalhos de numero e respostas finais ganham 'Menu'."""
    def botao(rotulo, valor):
        return {"text": rotulo[:60], "callback_data": f"{dono}|{valor}"}

    linhas = [[botao(f"{n}) {re.sub(r'[*]', '', rotulo)}", n)] for n, rotulo in OPCAO.findall(resposta)]
    if "Quantas unidades de" in resposta:
        linhas.append([botao(str(n), n) for n in (1, 2, 3, 4, 5)])
        linhas.append([botao(str(n), n) for n in (6, 8, 10, 12)])
    if not resposta.startswith("*PALACIO'S*"):
        linhas.append([botao("Menu", "menu")])  # saida sempre a um toque, em qualquer passo
    return linhas


def espera_texto(resposta):
    """True quando a resposta pede algo para digitar (nome, item...) e nao traz botoes de resposta."""
    return not (OPCAO.search(resposta) or "Quantas unidades de" in resposta
                or "para voltar ao início" in resposta or resposta.startswith("*PALACIO'S*"))


def _guardar_teclado(conn, chave, message_id):
    with conn.cursor() as cur:
        if message_id:
            cur.execute(
                """INSERT INTO whatsapp_estado (chave, dados, atualizado_em) VALUES (%s, %s, NOW())
                   ON CONFLICT (chave) DO UPDATE SET dados = EXCLUDED.dados, atualizado_em = NOW()""",
                (chave, str(message_id)),
            )
        else:
            cur.execute("DELETE FROM whatsapp_estado WHERE chave = %s", (chave,))
    conn.commit()


def _teclado_anterior(conn, chave):
    with conn.cursor() as cur:
        cur.execute("SELECT dados FROM whatsapp_estado WHERE chave = %s", (chave,))
        row = cur.fetchone()
    return int(row["dados"]) if row and row["dados"].isdigit() else None


def _responder(conn, chat_id, uid, resposta, grupo, responder_a, com_botoes=True):
    """Tira os botoes da resposta anterior (para nao tocar em botao velho) e envia a nova."""
    chave = f"tgkb:{chat_id}:{uid}"
    anterior = _teclado_anterior(conn, chave)
    if anterior:
        chamar("editMessageReplyMarkup", {"chat_id": chat_id, "message_id": anterior,
                                          "reply_markup": {"inline_keyboard": []}})
    botoes = teclado(resposta, uid) if com_botoes else []
    if grupo and espera_texto(resposta):
        resposta += "\n↩ Responda a esta mensagem para continuar."
    enviado = enviar_texto(chat_id, resposta, botoes or None, responder_a if grupo else None)
    _guardar_teclado(conn, chave, (enviado or {}).get("message_id") if botoes else None)


# ---------------------------------------------------------------- grupos

def _meu_usuario():
    if _usuario_bot["valor"] is None:
        resp = chamar("getMe") or {}
        _usuario_bot["valor"] = ((resp.get("result") or {}).get("username") or "").lower()
    return _usuario_bot["valor"]


def _id_do_bot():
    token = os.environ.get("TELEGRAM_TOKEN", "")
    return token.split(":")[0]


def texto_para_o_bot(msg):
    """Em grupo so vale: comando, mencao ao bot ou resposta a uma mensagem do bot.
    Devolve o texto ja sem a mencao, ou None para ignorar."""
    texto = msg.get("text")
    if texto is None:
        return None
    usuario = _meu_usuario()
    if texto.startswith("/"):
        alvo = re.match(r"^/\w+@(\w+)", texto)
        if alvo and alvo.group(1).lower() != usuario:
            return None
        return texto
    mencao = f"@{usuario}" if usuario else None
    if mencao and mencao in texto.lower():
        return re.sub(re.escape(mencao), "", texto, flags=re.I).strip()
    resposta_a = ((msg.get("reply_to_message") or {}).get("from") or {})
    if resposta_a.get("is_bot") and str(resposta_a.get("id")) == _id_do_bot():
        return texto
    return None


# ---------------------------------------------------------------- tratamento

def processar_update(update, abrir_conexao):
    """Trata uma mensagem ou um toque em botao. Nunca levanta erro (o Telegram reenvia se falhar)."""
    update_id = update.get("update_id")
    if update_id is None:
        return
    cb = update.get("callback_query")
    if cb:
        return _processar_toque(update_id, cb, abrir_conexao)

    msg = update.get("message")
    if not msg:
        return
    chat = msg.get("chat") or {}
    grupo = chat.get("type") in TIPOS_GRUPO
    if chat.get("type") != "private" and not grupo:
        return
    uid = str((msg.get("from") or {}).get("id", ""))
    if not uid or chat.get("id") is None:
        return
    texto = msg.get("text")
    if grupo:
        texto = texto_para_o_bot(msg)
        if texto is None:
            return  # conversa comum do grupo: o bot fica quieto
    _tratar(update_id, chat["id"], uid, texto, abrir_conexao, grupo, msg.get("message_id"),
            sem_texto=msg.get("text") is None)


def _processar_toque(update_id, cb, abrir_conexao):
    uid = str((cb.get("from") or {}).get("id", ""))
    dono, _, valor = (cb.get("data") or "").partition("|")
    msg = cb.get("message") or {}
    chat = msg.get("chat") or {}
    if not uid or chat.get("id") is None or not valor:
        chamar("answerCallbackQuery", {"callback_query_id": cb.get("id")})
        return
    if dono != uid:
        chamar("answerCallbackQuery", {"callback_query_id": cb.get("id"),
                                       "text": "Esse botão é de outra pessoa."})
        return
    chamar("answerCallbackQuery", {"callback_query_id": cb.get("id")})
    chamar("editMessageReplyMarkup", {"chat_id": chat["id"], "message_id": msg.get("message_id"),
                                      "reply_markup": {"inline_keyboard": []}})
    _tratar(update_id, chat["id"], uid, valor, abrir_conexao, chat.get("type") in TIPOS_GRUPO,
            msg.get("message_id"), rotulo=f"[botão] {valor}")


def _tratar(update_id, chat_id, uid, texto, abrir_conexao, grupo, responder_a, sem_texto=False, rotulo=None):
    operador = usuarios_permitidos().get(uid)
    msg_id = f"tg-{update_id}"

    conn = abrir_conexao()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """INSERT INTO whatsapp_mensagens (id, telefone, nome, texto, canal)
                   VALUES (%s, %s, %s, %s, 'telegram') ON CONFLICT (id) DO NOTHING RETURNING id""",
                (msg_id, uid, operador, rotulo or texto or "")
            )
            if cur.fetchone() is None:
                conn.commit()
                return  # repetida pelo Telegram: ja tratada
        conn.commit()

        if operador is None:
            resposta = (f"Este usuário ainda não está autorizado. O seu ID do Telegram é *{uid}*. "
                        "Mande esse número para quem cuida do app para ser liberado.")
        elif sem_texto or texto is None:
            resposta = "Por enquanto só entendo mensagens de texto."
        else:
            try:
                resposta = tratar_mensagem(conn, f"tg:{chat_id}:{uid}", operador,
                                           limpar_comando(texto), canal="Telegram")
                conn.commit()
            except Exception:
                conn.rollback()
                log.exception("Erro ao tratar mensagem do Telegram")
                resposta = "Não consegui processar essa mensagem. Nada foi registrado; tente de novo."

        with conn.cursor() as cur:
            cur.execute("UPDATE whatsapp_mensagens SET resposta = %s WHERE id = %s", (resposta, msg_id))
        conn.commit()
        _responder(conn, chat_id, uid, resposta, grupo, responder_a, com_botoes=operador is not None)
        if operador is not None:
            try:
                notificacoes.checar_estoque(conn)
            except Exception:
                conn.rollback()
                log.exception("Falha ao checar o estoque")
    except Exception:
        conn.rollback()
        log.exception("Falha no webhook do Telegram")
    finally:
        conn.close()
