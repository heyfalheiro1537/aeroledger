"""Conexão com o PostgreSQL e criação do esquema.

O esquema vem dentro do pacote (aeroledger/sql). A conexão usa, nesta ordem:
o argumento dsn, a variável AEROLEDGER_DSN e
o padrão local postgresql://aeroledger@localhost:54329/aeroledger.
"""
import os
from importlib import resources

import psycopg

DSN_PADRAO = "postgresql://aeroledger@localhost:54329/aeroledger"
ARQUIVOS = ("01_esquema.sql", "02_leitura.sql")
ARQUIVO_POSTGIS = "03_postgis.sql"


def dsn_padrao():
    return os.environ.get("AEROLEDGER_DSN") or DSN_PADRAO


def conectar(dsn=None):
    """Abre uma conexão (sem autocommit; cada escrita do ledger faz o próprio commit)."""
    return psycopg.connect(dsn or dsn_padrao(), autocommit=False)


def sql(nome):
    """Texto de um arquivo SQL do pacote."""
    return resources.files("aeroledger").joinpath("sql", nome).read_text(encoding="utf-8")


def criar_esquema(conn, postgis=False):
    """Cria tabelas, índices e funções de leitura. Idempotente.

    postgis=True também cria a tabela de geometria em PostGIS (opcional; exige a
    extensão instalada no servidor).
    """
    with conn.cursor() as cur:
        for a in ARQUIVOS + ((ARQUIVO_POSTGIS,) if postgis else ()):
            cur.execute(sql(a))
    conn.commit()
