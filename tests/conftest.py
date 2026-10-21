"""Cada teste roda num banco novo, clonado de um modelo com o esquema carregado.

Precisa de um PostgreSQL acessível em AEROLEDGER_DSN_ADMIN (padrão: o servidor local de
scripts/pg_local.sh). Os testes usam só o aeródromo fictício de
cenario_sintetico.py.
"""
import os
import secrets
import sys
from pathlib import Path

import psycopg
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))   # cenario_sintetico

from aeroledger import banco  # noqa: E402

ADMIN = os.environ.get("AEROLEDGER_DSN_ADMIN", "postgresql://aeroledger@localhost:54329/postgres")
MODELO = "aeroledger_modelo_testes"


def _admin():
    return psycopg.connect(ADMIN, autocommit=True)


@pytest.fixture(scope="session")
def modelo():
    with _admin() as a:
        a.execute(f"DROP DATABASE IF EXISTS {MODELO}")
        a.execute(f"CREATE DATABASE {MODELO}")
    base = ADMIN.rsplit("/", 1)[0]
    with psycopg.connect(f"{base}/{MODELO}") as c:
        banco.criar_esquema(c)
    yield base
    with _admin() as a:
        a.execute(f"DROP DATABASE IF EXISTS {MODELO}")


@pytest.fixture
def conn(modelo):
    nome = "aeroledger_t_" + secrets.token_hex(4)
    with _admin() as a:
        a.execute(f"CREATE DATABASE {nome} TEMPLATE {MODELO}")
    c = psycopg.connect(f"{modelo}/{nome}")
    yield c
    c.close()
    with _admin() as a:
        a.execute(f"DROP DATABASE {nome}")
