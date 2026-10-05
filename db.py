import os

import psycopg2
import psycopg2.extras
from dotenv import load_dotenv

load_dotenv()

DATABASE_URL = os.environ.get("DATABASE_URL")

CATEGORIAS = ("mercadinho", "restaurante")

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
    ativo BOOLEAN NOT NULL DEFAULT TRUE,
    categoria TEXT NOT NULL DEFAULT 'mercadinho',
    estoque DOUBLE PRECISION NOT NULL DEFAULT 0,
    custo DOUBLE PRECISION NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS pedidos (
    id SERIAL PRIMARY KEY,
    data DATE NOT NULL,
    pessoa_id INTEGER NOT NULL REFERENCES pessoas(id),
    produto_id INTEGER NOT NULL REFERENCES produtos(id),
    quantidade DOUBLE PRECISION NOT NULL,
    valor_unitario DOUBLE PRECISION NOT NULL,
    valor_total DOUBLE PRECISION NOT NULL,
    custo_unitario DOUBLE PRECISION NOT NULL DEFAULT 0,
    custo_total DOUBLE PRECISION NOT NULL DEFAULT 0,
    forma_pagamento TEXT,
    pago BOOLEAN NOT NULL DEFAULT FALSE,
    data_pagamento DATE,
    observacoes TEXT,
    criado_em TIMESTAMP NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS entradas_estoque (
    id SERIAL PRIMARY KEY,
    data DATE NOT NULL DEFAULT CURRENT_DATE,
    produto_id INTEGER NOT NULL REFERENCES produtos(id),
    quantidade DOUBLE PRECISION NOT NULL,
    observacoes TEXT,
    criado_em TIMESTAMP NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS whatsapp_mensagens (
    id TEXT PRIMARY KEY,
    telefone TEXT NOT NULL,
    nome TEXT,
    texto TEXT,
    resposta TEXT,
    criado_em TIMESTAMP NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS whatsapp_estado (
    chave TEXT PRIMARY KEY,
    dados TEXT NOT NULL,
    atualizado_em TIMESTAMP NOT NULL DEFAULT NOW()
);

ALTER TABLE produtos ADD COLUMN IF NOT EXISTS categoria TEXT NOT NULL DEFAULT 'mercadinho';
ALTER TABLE produtos ADD COLUMN IF NOT EXISTS estoque DOUBLE PRECISION NOT NULL DEFAULT 0;
ALTER TABLE produtos ADD COLUMN IF NOT EXISTS custo DOUBLE PRECISION NOT NULL DEFAULT 0;
ALTER TABLE pedidos ADD COLUMN IF NOT EXISTS custo_unitario DOUBLE PRECISION NOT NULL DEFAULT 0;
ALTER TABLE pedidos ADD COLUMN IF NOT EXISTS custo_total DOUBLE PRECISION NOT NULL DEFAULT 0;
"""

# constraint criada separadamente (IF NOT EXISTS para constraints exige checar antes)
CONSTRAINT_SQL = """
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint WHERE conname = 'produtos_categoria_check'
    ) THEN
        ALTER TABLE produtos ADD CONSTRAINT produtos_categoria_check
            CHECK (categoria IN ('mercadinho', 'restaurante'));
    END IF;
END $$;
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
        cur.execute(CONSTRAINT_SQL)
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
