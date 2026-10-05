"""Testes do leitor de mensagens do bot (nao usam banco nem rede).
Rode com: python tests_bot.py
"""
import whatsapp_bot as bot

PRODUTOS = [
    (1, "Coxinha", 8), (2, "Coca Cola lata 350ml", 7), (3, "Coca cola Pet 200ml", 4),
    (4, "Guaraná Antarctica Pet 200ml", 4), (5, "Torta", 7), (6, "Torta de Limão", 7),
    (7, "Suco Del Valle 290ml", 7), (8, "Suco Del Valle 200ml caxinha", 5), (9, "Suco Nat 180ml", 5),
    (10, "Canjica 200ml", 12), (11, "Canjica 300ml", 15), (12, "Pão de batata", 8),
    (13, "Brigadeiro de paçoca", 3), (14, "Monster 473ml", 12), (15, "Esfiha", 8),
]
PESSOAS = [
    (1, "KEVIN"), (2, "KARINA PREPARAÇÃO"), (3, "KARINA ENCARREGADA"), (4, "PAULA PCP"),
    (5, "ANDRESSA (SIMONE M)"), (6, "ANDRESSA PCP"), (7, "FERNANDA ALMOXARIFADO"), (8, "DAVI"),
]
produtos = [{"id": i, "nome": n, "preco": p} for i, n, p in PRODUTOS]
pessoas = [{"id": i, "nome": n} for i, n in PESSOAS]


def ler(texto):
    return bot.montar_estado(texto, pessoas, produtos)


def resumo(estado):
    p = estado["pessoa"]
    if p is None:
        nome = None
    elif "id" in p:
        nome = p["nome"]
    elif "candidatos" in p:
        nome = "?" + "|".join(c["nome"] for c in p["candidatos"])
    else:
        nome = "NOVO:" + p["novo"]
    itens = []
    for it in estado["itens"]:
        if it.get("produto"):
            itens.append((it["qtd"], it["produto"]["nome"]))
        else:
            itens.append((it["qtd"], "?" + "|".join(c["nome"] for c in it["candidatos"])))
    return nome, itens


CASOS = [
    ("Kevin 2 coxinhas e 1 guaraná", "KEVIN", [(2, "Coxinha"), (1, "Guaraná Antarctica Pet 200ml")]),
    ("kevin: 2 coxinha, 1 suco nat", "KEVIN", [(2, "Coxinha"), (1, "Suco Nat 180ml")]),
    ("2 coxinhas pro Kevin", "KEVIN", [(2, "Coxinha")]),
    ("duas coxinhas pra davi", "DAVI", [(2, "Coxinha")]),
    ("davi coxinha", "DAVI", [(1, "Coxinha")]),
    ("coxinha davi", "DAVI", [(1, "Coxinha")]),
    ("2 coxinhas e 1 esfiha kevin", "KEVIN", [(2, "Coxinha"), (1, "Esfiha")]),
    ("kevin 2x coxinha", "KEVIN", [(2, "Coxinha")]),
    ("kevin uma duzia de brigadeiro de pacoca", "KEVIN", [(12, "Brigadeiro de paçoca")]),
    ("kevin coca cola lata", "KEVIN", [(1, "Coca Cola lata 350ml")]),
    ("kevin 1 canjica 300ml", "KEVIN", [(1, "Canjica 300ml")]),
    ("kevin 1 pao de batata", "KEVIN", [(1, "Pão de batata")]),
    ("kevin 1 coxina", "KEVIN", [(1, "Coxinha")]),
    ("kevin 3 torta", "KEVIN", [(3, "Torta")]),
    ("kevin 1 monster", "KEVIN", [(1, "Monster 473ml")]),
    ("paula 2 coxinhas", "PAULA PCP", [(2, "Coxinha")]),
    ("fernanda 1 esfiha", "FERNANDA ALMOXARIFADO", [(1, "Esfiha")]),
    ("lucas 1 coxinha", "NOVO:LUCAS", [(1, "Coxinha")]),
    ("2 coxinhas", None, [(2, "Coxinha")]),
    ("karina 1 coxinha", "?KARINA PREPARAÇÃO|KARINA ENCARREGADA", [(1, "Coxinha")]),
    ("andressa pcp 1 coxinha", "ANDRESSA PCP", [(1, "Coxinha")]),
    ("kevin 1 coca", "KEVIN", [(1, "?Coca Cola lata 350ml|Coca cola Pet 200ml")]),
    ("kevin 1 canjica", "KEVIN", [(1, "?Canjica 200ml|Canjica 300ml")]),
]

falhas = 0
for texto, pessoa_esperada, itens_esperados in CASOS:
    estado, nao_entendidos = ler(texto)
    nome, itens = resumo(estado)
    ok = nome == pessoa_esperada and itens == itens_esperados and not nao_entendidos
    if not ok:
        falhas += 1
        print(f"FALHOU: {texto!r}\n   obtido:   {nome} {itens} nao_entendidos={nao_entendidos}"
              f"\n   esperado: {pessoa_esperada} {itens_esperados}")

estado, nao = ler("kevin 1 pizza de marte")
if not nao:
    falhas += 1
    print("FALHOU: produto inexistente deveria ser recusado")

assert bot.chave_telefone("+55 (11) 98765-4321") == bot.chave_telefone("551187654321"), "9o digito"
assert bot.chave_telefone("5511987654321") == "1187654321"

print(f"{len(CASOS) + 1} casos, {falhas} falha(s)")
raise SystemExit(1 if falhas else 0)
