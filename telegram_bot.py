"""Bot do Telegram: o mesmo menu e passo a passo do WhatsApp, pela API gratuita de bots.

Variaveis de ambiente (configuradas no Render, nunca no codigo):
  TELEGRAM_TOKEN           token que o @BotFather entrega ao criar o bot
  TELEGRAM_WEBHOOK_SECRET  frase secreta (so letras, numeros, _ e -) que voce inventa;
                           o Telegram devolve ela em cada mensagem para provar a origem
  TELEGRAM_USUARIOS        quem pode usar: 123456789:Yan,987654321:Maria  (ID numerico:nome)
"""
import hmac
import html
import json
import logging
import os
import re
import urllib.error
import urllib.request

from whatsapp_bot import tratar_mensagem

log = logging.getLogger("telegram")


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
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as e:
        try:
            return json.loads(e.read())
        except Exception:
            return {"ok": False, "description": f"HTTP {e.code}"}
    except Exception as e:
        log.exception("Falha ao chamar o Telegram")
        return {"ok": False, "description": str(e)}


def enviar_texto(chat_id, texto):
    resp = chamar("sendMessage", {"chat_id": chat_id, "text": para_html(texto[:3800]),
                                  "parse_mode": "HTML", "disable_web_page_preview": True})
    if resp is None:
        log.warning("TELEGRAM_TOKEN nao configurado; resposta nao enviada")
        return False
    if not resp.get("ok"):
        log.error("Telegram recusou o envio: %s", resp.get("description"))
    return bool(resp.get("ok"))


def ativar_webhook(url):
    """Diz ao Telegram para entregar as mensagens neste endereco."""
    return chamar("setWebhook", {"url": url, "secret_token": os.environ.get("TELEGRAM_WEBHOOK_SECRET", ""),
                                 "allowed_updates": ["message"]})


def limpar_comando(texto):
    """'/menu@meubot' vira 'menu'; '/start' vira 'start'."""
    return re.sub(r"^/(\w+)(@\w+)?", r"\1", (texto or "").strip())


def processar_update(update, abrir_conexao):
    """Trata uma mensagem recebida. Nunca levanta erro (o Telegram reenvia se o webhook falhar)."""
    msg = update.get("message")
    update_id = update.get("update_id")
    if not msg or update_id is None:
        return
    chat = msg.get("chat") or {}
    if chat.get("type") != "private":
        return  # grupos ficam para depois
    uid = str((msg.get("from") or {}).get("id", ""))
    chat_id = chat.get("id")
    if not uid or chat_id is None:
        return
    texto = msg.get("text")
    operador = usuarios_permitidos().get(uid)
    msg_id = f"tg-{update_id}"

    conn = abrir_conexao()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """INSERT INTO whatsapp_mensagens (id, telefone, nome, texto, canal)
                   VALUES (%s, %s, %s, %s, 'telegram') ON CONFLICT (id) DO NOTHING RETURNING id""",
                (msg_id, uid, operador, texto or ""),
            )
            if cur.fetchone() is None:
                conn.commit()
                return  # repetida pelo Telegram: ja tratada
        conn.commit()

        if operador is None:
            resposta = (f"Este usuário ainda não está autorizado. O seu ID do Telegram é *{uid}*. "
                        "Mande esse número para quem cuida do app para ser liberado.")
        elif texto is None:
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
        enviar_texto(chat_id, resposta)
    except Exception:
        conn.rollback()
        log.exception("Falha no webhook do Telegram")
    finally:
        conn.close()
