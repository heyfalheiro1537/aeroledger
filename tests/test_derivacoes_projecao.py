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


def test_premissa_ausente_recusa_em_vez_de_zerar(conn):
    run = montar(conn, com_cabeceiras=False)
    d = derivacoes.distancias_declaradas(conn, run)
    assert abs(d["05"]["TORA"] - 2000) < 1e-6                         # só depende da pista
    assert set(d["05"]) == {"TORA", "recusado"}
    assert set(d["05"]["recusado"]) == {"TODA", "ASDA", "LDA"}
    for fid, desig in (("XXXX-THR05", "05"), ("XXXX-THR23", "23")):  # cabeceiras sem declarações
        ledger.append_event(conn, run, fid, "runway_direction", "BASELINE", asserted_by="teste",
                            corpo={"adc": {"designador": desig, "pista": "XXXX-RWY"}})
    d = derivacoes.distancias_declaradas(conn, run)
    assert "deslocamento_m" in d["05"]["recusado"]["LDA"]
    assert "tem_swy" in d["05"]["recusado"]["ASDA"] and "tem_cwy" in d["05"]["recusado"]["TODA"]


def test_declaracao_contraditoria_recusa(conn):
    run = montar(conn)                                                  # SWY registrada para a 05
    ledger.append_event(conn, run, "XXXX-THR05", "runway_direction", "DELTA", asserted_by="teste",
                        alteracoes={"adc.tem_swy": False}, reason="teste de contradição")
    d = derivacoes.distancias_declaradas(conn, run)
    assert "ASDA" not in d["05"] and "declara que não há" in d["05"]["recusado"]["ASDA"]
    ledger.append_event(conn, run, "XXXX-THR23", "runway_direction", "DELTA", asserted_by="teste",
                        alteracoes={"adc.tem_cwy": True}, reason="teste de contradição")
    d = derivacoes.distancias_declaradas(conn, run)
    assert "não registrada" in d["23"]["recusado"]["TODA"]
    assert abs(d["23"]["LDA"] - 2000) < 1e-6                           # o resto segue calculado


def test_deslocamento_declarado_reduz_a_lda(conn):
    run = montar(conn)
    ledger.append_event(conn, run, "XXXX-THR23", "runway_direction", "DELTA", asserted_by="teste",
                        alteracoes={"adc.deslocamento_m": 150}, reason="cabeceira deslocada")
    d = derivacoes.distancias_declaradas(conn, run)
    assert abs(d["23"]["LDA"] - 1850) < 1e-6 and abs(d["23"]["TORA"] - 2000) < 1e-6
    assert "recusado" not in d["23"]


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
