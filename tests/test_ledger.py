"""Forma do ledger: rejeições, relógio, instante, diff e integridade básica."""
from datetime import timedelta

import pytest

from aeroledger import ledger, relogio
from aeroledger.ledger import LedgerError, REMOVIDO
from cenario_sintetico import ORIGEM


@pytest.fixture
def run(conn):
    return relogio.novo_run(conn, ORIGEM)


def base(conn, run, fid="F", tipo="taxiway", **kw):
    return ledger.append_event(conn, run, fid, tipo, "BASELINE", asserted_by="t", corpo={"a": 1}, **kw)


@pytest.mark.parametrize("caso", [
    "delta_sem_baseline", "baseline_sobre_viva", "tipo_muda", "owner_muda", "void_sem_motivo",
    "void_com_corpo", "path_malformado", "remocao_de_ausente", "folha_e_prefixo",
    "caused_by_inexistente", "asserted_by_vazio", "tipo_invalido",
])
def test_rejeicoes(conn, run, caso):
    if caso != "delta_sem_baseline":
        base(conn, run)
    chamadas = {
        "delta_sem_baseline": lambda: ledger.append_event(conn, run, "G", "taxiway", "DELTA", asserted_by="t", alteracoes={"a": 2}),
        "baseline_sobre_viva": lambda: base(conn, run),
        "tipo_muda": lambda: ledger.append_event(conn, run, "F", "apron", "DELTA", asserted_by="t", alteracoes={"a": 2}),
        "owner_muda": lambda: ledger.append_event(conn, run, "F", "taxiway", "DELTA", asserted_by="t", alteracoes={"a": 2}, owner_ref="X"),
        "void_sem_motivo": lambda: ledger.append_event(conn, run, "F", "taxiway", "VOID", asserted_by="t"),
        "void_com_corpo": lambda: ledger.append_event(conn, run, "F", "taxiway", "VOID", asserted_by="t", reason="x", alteracoes={"a": 1}),
        "path_malformado": lambda: ledger.append_event(conn, run, "F", "taxiway", "DELTA", asserted_by="t", alteracoes={"A b": 1}),
        "remocao_de_ausente": lambda: ledger.append_event(conn, run, "F", "taxiway", "DELTA", asserted_by="t", alteracoes={"z": REMOVIDO}),
        "folha_e_prefixo": lambda: ledger.append_event(conn, run, "F", "taxiway", "DELTA", asserted_by="t", alteracoes={"a.b": 1}),
        "caused_by_inexistente": lambda: ledger.append_event(conn, run, "F", "taxiway", "DELTA", asserted_by="t", alteracoes={"a": 2}, caused_by=123),
        "asserted_by_vazio": lambda: ledger.append_event(conn, run, "F", "taxiway", "DELTA", asserted_by="", alteracoes={"a": 2}),
        "tipo_invalido": lambda: ledger.append_event(conn, run, "F", "taxiway", "TEMPDELTA", asserted_by="t", alteracoes={"a": 2}),
    }
    n_antes = _n(conn)
    with pytest.raises(LedgerError):
        chamadas[caso]()
    conn.rollback()
    assert _n(conn) == n_antes                       # nada foi escrito


def _n(conn):
    with conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM event")
        return cur.fetchone()[0]


def test_relogio_so_avanca(conn, run):
    with pytest.raises(relogio.RelogioError):
        relogio.avancar(conn, run, dias=-1)
    e1, _ = base(conn, run)
    e2, _ = ledger.append_event(conn, run, "F", "taxiway", "DELTA", asserted_by="t", alteracoes={"a": 2})
    assert e2 > e1                                   # mesmo instante: a sequência ordena
    assert relogio.instante(e1) == relogio.instante(e2) == ORIGEM
    relogio.avancar(conn, run, dias=3)
    e3, _ = ledger.append_event(conn, run, "F", "taxiway", "DELTA", asserted_by="t", alteracoes={"a": 3})
    assert relogio.instante(e3) == ORIGEM + timedelta(days=3)


def test_t_por_instante_ou_por_evento(conn, run):
    e1, _ = base(conn, run)
    relogio.avancar(conn, run, dias=2)
    ledger.append_event(conn, run, "F", "taxiway", "DELTA", asserted_by="t", alteracoes={"a": 2})
    # ao fim do instante da origem, o estado é o do primeiro evento
    assert ledger.reconstruct_state(conn, run, "F", ORIGEM) == ledger.reconstruct_state(conn, run, "F", e1) == {"a": 1}
    assert ledger.reconstruct_state(conn, run, "F", ORIGEM + timedelta(days=2)) == {"a": 2}
    assert ledger.reconstruct_state(conn, run, "F", ORIGEM - timedelta(seconds=1)) is None


def test_diff_e_causa(conn, run):
    e1, _ = base(conn, run)
    relogio.avancar(conn, run, dias=1)
    e2, _ = ledger.append_event(conn, run, "F", "taxiway", "DELTA", asserted_by="op", reason="obra",
                                alteracoes={"a": 2, "b": "novo"}, caused_by=e1)
    d = ledger.diff(conn, run, e1, e2)
    assert ("F", "a", 1, 2) in d and ("F", "b", ledger.AUSENTE, "novo") in d and len(d) == 2
    env = ledger.envelope(conn, e2)
    assert env["asserted_by"] == "op" and env["caused_by"] == e1 and env["reason"] == "obra"


def test_cadeia_integra_e_conteudo_alterado(conn, run):
    """Alterar o conteúdo de um evento quebra a cadeia naquele evento."""
    base(conn, run)
    for i in range(5):
        relogio.avancar(conn, run, horas=1)
        ledger.append_event(conn, run, "F", "taxiway", "DELTA", asserted_by="t", alteracoes={"a": i})
    assert ledger.verify_chain(conn, run) == []
    with conn.cursor() as cur:
        cur.execute("SELECT event_id FROM event ORDER BY event_id OFFSET 2 LIMIT 1")
        alvo = cur.fetchone()[0]
        cur.execute("UPDATE event_field SET next = '99'::jsonb WHERE event_id = %s", (alvo,))
    conn.commit()
    assert ledger.verify_chain(conn, run) == [alvo]


def test_canonico():
    from aeroledger.canonico import canonico
    assert canonico({"b": 1, "a": [1.0, 2.5, None, True]}) == b'{"a":[1,2.5,null,true],"b":1}'
    assert canonico({"\u20ac": 1, "\r": 2, "1": 3}) == '{"\\r":2,"1":3,"\u20ac":1}'.encode()
    # números pelo ECMAScript (RFC 8785): dígitos mais curtos, também acima de 2^53
    for x, esperado in [(1e21, "1e+21"), (1e20, "100000000000000000000"), (1e-7, "1e-7"),
                        (1.5e-6, "0.0000015"), (5.1521516039062726e+17, "515215160390627260")]:
        assert canonico(x) == esperado.encode()
    with pytest.raises(ValueError):
        canonico(float("nan"))
