"""Avisos automaticos pelo Telegram: estoque baixo, resumo do dia e backup semanal.

O Render free "dorme" e nao tem agendador. Por isso as tarefas rodam quando alguem acessa
/saude (um monitor gratuito como o UptimeRobot acessa a cada 5 minutos). Cada aviso e
reservado na tabela `avisos` antes de ser enviado, entao nunca sai duplicado.
"""
import logging
import os

import servicos

log = logging.getLogger("notificacoes")


def _telegram():
    import telegram_bot  # import tardio: telegram_bot tambem importa este modulo
    return telegram_bot


def destinatarios():
    ids = [x.strip() for x in os.environ.get("TELEGRAM_AVISOS", "").split(",") if x.strip().isdigit()]
    return ids or list(_telegram().usuarios_permitidos())[:1]


def ativo():
    return bool(os.environ.get("TELEGRAM_TOKEN")) and bool(destinatarios())


def avisar(texto):
    enviado = False
    for chat_id in destinatarios():
        if _telegram().enviar_texto(int(chat_id), texto) is not None:
            enviado = True
    return enviado


def _reservar(conn, chave):
    """True so para quem conseguiu reservar o aviso (evita enviar em dobro)."""
    with conn.cursor() as cur:
        cur.execute("INSERT INTO avisos (chave) VALUES (%s) ON CONFLICT (chave) DO NOTHING RETURNING chave",
                    (chave,))
        ok = cur.fetchone() is not None
    conn.commit()
    return ok


def _liberar(conn, chave):
    """Devolve a reserva quando o envio falhou, para tentar de novo no proximo acesso."""
    with conn.cursor() as cur:
        cur.execute("DELETE FROM avisos WHERE chave = %s", (chave,))
    conn.commit()


def checar_estoque(conn):
    """Avisa uma vez quando um produto cai ate o minimo; avisa de novo so depois de ser reposto."""
    with conn.cursor() as cur:
        cur.execute("SELECT id, nome, estoque FROM produtos WHERE ativo AND estoque <= %s ORDER BY nome",
                    (servicos.ESTOQUE_MINIMO,))
        baixos = cur.fetchall()
        cur.execute("SELECT chave FROM avisos WHERE chave LIKE 'estoque:%'")
        avisados = {r["chave"] for r in cur.fetchall()}
        atuais = {f"estoque:{p['id']}" for p in baixos}
        for chave in avisados - atuais:
            cur.execute("DELETE FROM avisos WHERE chave = %s", (chave,))
        novos = [p for p in baixos if f"estoque:{p['id']}" not in avisados]
        if novos and ativo():
            for p in novos:
                cur.execute("INSERT INTO avisos (chave) VALUES (%s) ON CONFLICT (chave) DO NOTHING",
                            (f"estoque:{p['id']}",))
        else:
            novos = []
    conn.commit()
    if novos:
        lista = ", ".join(f"{p['nome']} ({servicos.qtd_texto(p['estoque'])})" for p in novos)
        avisar(f"*Estoque baixo*: {lista}.")


def enviar_backup(conn=None):
    """Manda o Excel de backup para quem recebe os avisos. Devolve True se enviou."""
    from export_xlsx import exportar_bytes
    conteudo = exportar_bytes()
    nome = f"palacios_backup_{servicos.agora():%Y%m%d}.xlsx"
    enviou = False
    for chat_id in destinatarios():
        if _telegram().enviar_documento(int(chat_id), conteudo, nome, "Backup do PALACIO'S (Excel)"):
            enviou = True
    return enviou


def rodar_tarefas(abrir_conexao):
    """Resumo do dia, backup semanal e alerta de estoque. Seguro de chamar a toda hora."""
    if not ativo():
        return
    conn = abrir_conexao()
    try:
        agora = servicos.agora()
        hora = int(os.environ.get("RESUMO_HORA", "20") or 20)
        if agora.hour >= hora:
            if _reservar(conn, f"resumo:{agora.date()}"):
                with conn.cursor() as cur:
                    texto = servicos.texto_resumo(cur)
                if not avisar(texto):
                    _liberar(conn, f"resumo:{agora.date()}")
            with conn.cursor() as cur:
                cur.execute("SELECT 1 FROM avisos WHERE chave LIKE 'backup:%' "
                            "AND criado_em > NOW() - INTERVAL '7 days'")
                recente = cur.fetchone() is not None
            if not recente and _reservar(conn, f"backup:{agora.date()}"):
                if not enviar_backup():
                    _liberar(conn, f"backup:{agora.date()}")
        with conn.cursor() as cur:
            cur.execute("DELETE FROM avisos WHERE criado_em < NOW() - INTERVAL '90 days' "
                        "AND (chave LIKE 'resumo:%' OR chave LIKE 'backup:%')")
        conn.commit()
        checar_estoque(conn)
    except Exception:
        conn.rollback()
        log.exception("Falha ao rodar as tarefas automaticas")
    finally:
        conn.close()
