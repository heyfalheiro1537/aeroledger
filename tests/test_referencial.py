"""Referencial na forma B: as três regras e a integração com o ledger."""
import pytest

from aeroledger import ledger, referencial, relogio
from aeroledger.ledger import LedgerError
from aeroledger.referencial import ReferencialError, Resolvedor
from cenario_sintetico import A, B, GEOD, corpo_ref, montar, seed


def test_regra_1_georreferenciado_ou_relativo():
    c = corpo_ref(extra={"X": {"lat": 0, "lon": 0, "elemento": "RWY_A_B", "de": "THR_A", "s": 1, "o": 0}})
    with pytest.raises(ReferencialError, match="regra 1"):
        referencial.validar_corpo(c)


def test_regra_2_sem_ciclos():
    c = corpo_ref()
    c["referentes"]["P"] = {"elemento": "E2", "de": "Q", "s": 10, "o": 0}
    c["referentes"]["Q"] = {"elemento": "E2", "de": "P", "s": 10, "o": 0}
    c["elementos_lineares"]["E2"] = ["P", "Q"]
    with pytest.raises(ReferencialError, match="regra 2"):
        referencial.validar_corpo(c)


def test_regra_3_ponto_sozinho_nao_define_direcao():
    with pytest.raises(ReferencialError, match="regra 3"):
        referencial.validar_corpo(corpo_ref(extra={"X": {"de": "THR_A", "s": 10}}))
    with pytest.raises(ReferencialError, match="regra 3"):
        c = corpo_ref(extra={"Y": {"lat": 1, "lon": 1}, "X": {"elemento": "RWY_A_B", "de": "Y", "s": 10, "o": 0}})
        referencial.validar_corpo(c)


def test_ref_id_endereca_o_conteudo(conn):
    r1 = referencial.publicar(conn, corpo_ref())
    r2 = referencial.publicar(conn, corpo_ref())
    assert r1 == r2 and len(r1) == 64
    assert referencial.verificar_referenciais(conn) == []
    with conn.cursor() as cur:
        cur.execute("UPDATE referencial SET corpo = jsonb_set(corpo, '{datum}', '\"WGS84\"')")
    conn.commit()
    assert referencial.verificar_referenciais(conn) == [r1]


def test_referente_relativo_e_seed(conn):
    c = corpo_ref(extra={"MEIO": {"elemento": "RWY_A_B", "de": "THR_A", "s": 1000, "o": 0}})
    res = Resolvedor(c)
    lat, lon = res.referente("MEIO")
    _, _, d = GEOD.inv(A[1], A[0], lon, lat)
    assert abs(d - 1000) < 1e-6
    # seed a partir do relativo, no mesmo sentido do 'de'
    p = res.seed(seed("MEIO", s=500))
    _, _, d2 = GEOD.inv(A[1], A[0], p[1], p[0])
    assert abs(d2 - 1500) < 1e-6
    # afastamento à direita de quem anda de A para B (azimute 45°) fica a sudeste
    q = res.seed(seed("THR_A", s=0, o=10))
    az, _, d3 = GEOD.inv(A[1], A[0], q[1], q[0])
    assert abs(d3 - 10) < 1e-6 and abs((az % 360) - 135) < 1e-6


def test_seed_para_referente_inexistente_e_recusada(conn):
    run = montar(conn, com_swy=False)
    with pytest.raises(LedgerError, match="inexistente"):
        ledger.append_event(conn, run, "T", "taxiway", "BASELINE", asserted_by="t",
                            corpo={"geometria": {"primitivos": {"p": {"tipo": "ponto", "seed": seed("NAO_EXISTE")}}}})


def test_trocar_referencial_nao_pode_deixar_seed_orfa(conn):
    run = montar(conn)
    c = corpo_ref()
    c["referentes"]["THR_C"] = c["referentes"].pop("THR_B")
    c["elementos_lineares"]["RWY_A_B"] = ["THR_A", "THR_C"]
    novo = referencial.publicar(conn, c)
    with pytest.raises(LedgerError, match="seed"):
        ledger.append_event(conn, run, "XXXX", "airport", "DELTA", asserted_by="t",
                            alteracoes={"referencial.ref_id": novo}, reason="remedição")
