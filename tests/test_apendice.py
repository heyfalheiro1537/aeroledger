"""Testes de verificação do apêndice: campo ausente, snapshot, ciclo de vida.

Verificações do mecanismo de leitura.
"""
import random

import pytest

from aeroledger import ledger, relogio
from aeroledger.ledger import REMOVIDO
from cenario_sintetico import ORIGEM


def _run(conn):
    return relogio.novo_run(conn, ORIGEM)


def test_campo_ausente_nao_e_null(conn):
    run = _run(conn)
    e1, _ = ledger.append_event(conn, run, "F", "incident", "BASELINE", asserted_by="t",
                                corpo={"observacao": {"tipo": "fod"}})
    relogio.avancar(conn, run, dias=1)
    e2, _ = ledger.append_event(conn, run, "F", "incident", "DELTA", asserted_by="t",
                                alteracoes={"observacao.gravidade": None})
    relogio.avancar(conn, run, dias=1)
    e3, _ = ledger.append_event(conn, run, "F", "incident", "DELTA", asserted_by="t",
                                alteracoes={"observacao.gravidade": REMOVIDO})

    antes = ledger.reconstruct_state(conn, run, "F", e1)
    com_null = ledger.reconstruct_state(conn, run, "F", e2)
    removido = ledger.reconstruct_state(conn, run, "F", e3)

    assert "gravidade" not in antes["observacao"]           # ausente: a chave não existe
    assert "gravidade" in com_null["observacao"]            # null: a chave existe
    assert com_null["observacao"]["gravidade"] is None
    assert "gravidade" not in removido["observacao"]        # removido volta a ser ausente


def test_ciclo_de_vida(conn):
    run = _run(conn)
    b1, _ = ledger.append_event(conn, run, "N", "restriction", "BASELINE", asserted_by="t",
                                corpo={"texto": "trecho interditado", "antigo": 1})
    relogio.avancar(conn, run, dias=1)
    v1, _ = ledger.append_event(conn, run, "N", "restriction", "VOID", asserted_by="t", reason="obra concluída")
    relogio.avancar(conn, run, dias=1)
    entre = relogio.limite(relogio.agora(conn, run))
    relogio.avancar(conn, run, dias=1)
    b2, _ = ledger.append_event(conn, run, "N", "restriction", "BASELINE", asserted_by="t",
                                corpo={"texto": "nova interdição"})

    seg = ledger.ciclos(conn, run, "N")
    assert [c for _, _, c in seg] == [1, 1, 2]
    assert ledger.reconstruct_state(conn, run, "N", entre) is None
    novo = ledger.reconstruct_state(conn, run, "N", b2)
    assert novo == {"texto": "nova interdição"}               # o ciclo novo não herda campos
    assert ledger.reconstruct_state(conn, run, "N", b1)["antigo"] == 1


def test_snapshot_igual_a_reconstrucao(conn):
    """Snapshot é cache: com e sem ele, o estado é o mesmo em todo instante."""
    rng = random.Random(20261020)
    run = _run(conn)
    ledger.append_event(conn, run, "S", "taxiway", "BASELINE", asserted_by="t",
                        corpo={"a": {"x": 0}, "b": 0})
    instantes = []
    vivos = {"a.x", "b"}
    for i in range(60):
        relogio.avancar(conn, run, horas=rng.randint(1, 48))
        if rng.random() < 0.15 and len(vivos) > 1:
            alvo = rng.choice(sorted(vivos))
            alt = {alvo: REMOVIDO}
            vivos.discard(alvo)
        else:
            alvo = rng.choice(["a.x", "a.y", "b", "c.d"])
            alt = {alvo: rng.choice([i, None, f"v{i}", [i, i + 1]])}
            vivos.add(alvo)
        eid, _ = ledger.append_event(conn, run, "S", "taxiway", "DELTA", asserted_by="t", alteracoes=alt)
        instantes.append(eid)
        if rng.random() < 0.2:
            ledger.gravar_snapshot(conn, run, "S", eid)
    for eid in instantes:
        sem = ledger.reconstruct_state(conn, run, "S", eid, plano=True)
        com = ledger.reconstruct_state(conn, run, "S", eid, plano=True, usar_snapshot=True)
        assert sem == com
        for pref in ("a.", "c."):
            assert ledger.reconstruct_state(conn, run, "S", eid, prefixo=pref, plano=True) == \
                ledger.reconstruct_state(conn, run, "S", eid, prefixo=pref, plano=True, usar_snapshot=True)
    with conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM snapshot")
        assert cur.fetchone()[0] > 0
