import os

import psycopg2
import psycopg2.extras
from dotenv import load_dotenv

load_dotenv()

DATABASE_URL = os.environ.get("DATABASE_URL")

SCHEMA = """
CREATE TABLE IF NOT EXISTS pessoas (
    id SERIAL PRIMARY KEY,
    nome TEXT NOT NULL UNIQUE
);

CREATE TABLE IF NOT EXISTS produtos (
    id SERIAL PRIMARY KEY,
    numero_item INTEGER UNIQUE,
    nome TEXT NOT NULL,
    preco DOUBLE PRECISION NOT NULL,
    ativo BOOLEAN NOT NULL DEFAULT TRUE
);

CREATE TABLE IF NOT EXISTS pedidos (
    id SERIAL PRIMARY KEY,
    data DATE NOT NULL,
    pessoa_id INTEGER NOT NULL REFERENCES pessoas(id),
    produto_id INTEGER NOT NULL REFERENCES produtos(id),
    quantidade DOUBLE PRECISION NOT NULL,
    valor_unitario DOUBLE PRECISION NOT NULL,
    valor_total DOUBLE PRECISION NOT NULL,
    forma_pagamento TEXT,
    pago BOOLEAN NOT NULL DEFAULT FALSE,
    data_pagamento DATE,
    observacoes TEXT,
    criado_em TIMESTAMP NOT NULL DEFAULT NOW()
);
"""


def get_connection():
    if not DATABASE_URL:
        raise RuntimeError(
            "DATABASE_URL nao configurada. Crie um arquivo .env (veja .env.example) "
            "com a connection string do Supabase."
        )
    conn = psycopg2.connect(DATABASE_URL, cursor_factory=psycopg2.extras.RealDictCursor)
    return conn


def init_db():
    conn = get_connection()
    with conn, conn.cursor() as cur:
        cur.execute(SCHEMA)
    conn.close()


def get_or_create_pessoa(conn, nome):
    nome = nome.strip()
    with conn.cursor() as cur:
        cur.execute("SELECT id FROM pessoas WHERE nome ILIKE %s", (nome,))
        row = cur.fetchone()
        if row:
            return row["id"]
        cur.execute("INSERT INTO pessoas (nome) VALUES (%s) RETURNING id", (nome,))
        return cur.fetchone()["id"]
