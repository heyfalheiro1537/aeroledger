"""Derivações (fictício) e cache de projeção por marca d'água."""
import pytest

from aeroledger import derivacoes, ledger, projecao, referencial, relogio
from cenario_sintetico import corpo_ref, montar, seed


def test_comprimento_azimute_rumo():
    c = corpo_ref()
    assert abs(derivacoes.comprimento(c, "RWY_A_B") - 2000) < 1e-6
    az = derivacoes.azimutes(c, "RWY_A_B")
    assert abs(az["THR_A"] - 45) < 1e-9
    assert abs(derivacoes.rumo_magnetico(az["THR_A"], -20) - 65) < 1e-9


def test_distancias_declaradas(conn):
    run = montar(conn)
    d = derivacoes.distancias_declaradas(conn, run)
    assert abs(d["05"]["TORA"] - 2000) < 1e-6 and abs(d["05"]["ASDA"] - 2200) < 1e-6
    assert abs(d["23"]["ASDA"] - 2000) < 1e-6 and abs(d["23"]["LDA"] - 2000) < 1e-6
    # nada disso foi escrito
    with conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM event_field WHERE path ~ '(tora|toda|asda|lda|comprimento)'")
        assert cur.fetchone()[0] == 0


def test_barreira_recusa_em_vez_de_adivinhar(conn):
    run = montar(conn)
    ledger.append_event(conn, run, "XXXX-AG", "arresting_gear", "BASELINE", asserted_by="op",
                        corpo={"geometria": {"primitivos": {"p": {"tipo": "ponto", "seed": seed("THR_B", s=50)}}}})
    with pytest.raises(derivacoes.RegraPendente):
        derivacoes.distancias_declaradas(conn, run)


def test_cache_de_projecao(conn):
    run = montar(conn)
    tipos = ["runway", "protect_area"]
    c1, i1 = projecao.projetar(conn, run, tipos=tipos)
    c2, i2 = projecao.projetar(conn, run, tipos=tipos)
    assert i1["cache"] == "miss" and i2["cache"] == "hit" and c1 == c2
    # evento fora da seleção não derruba o cache
    relogio.avancar(conn, run, dias=1)
    ledger.append_event(conn, run, "INC-1", "incident", "BASELINE", asserted_by="t", corpo={"tipo": "fod"})
    _, i3 = projecao.projetar(conn, run, tipos=tipos)
    assert i3["cache"] == "hit" and i3["hwm"] == i1["hwm"]
    # evento dentro da seleção derruba
    ledger.append_event(conn, run, "XXXX-RWY", "runway", "DELTA", asserted_by="t", alteracoes={"adc.superficie": "asfalto"})
    c4, i4 = projecao.projetar(conn, run, tipos=tipos)
    assert i4["cache"] == "miss" and c4["XXXX-RWY"]["estado"]["adc"]["superficie"] == "asfalto"
    fresco, _ = projecao.projetar(conn, run, tipos=tipos, usar_cache=False)
    assert fresco == c4


def test_projecao_por_camada(conn):
    run = montar(conn)
    c, _ = projecao.projetar(conn, run, tipos=["runway"], camadas=["geometria"])
    assert set(c["XXXX-RWY"]["estado"]) == {"geometria"}
