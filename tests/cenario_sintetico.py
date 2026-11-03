"""Aeródromo fictício para os testes do artefato.

Coordenadas inventadas (perto de 10°S 50°W), sem relação com o SBSJ. Serve só
para verificar o mecanismo.
"""
from datetime import datetime, timezone

from pyproj import Geod

from aeroledger import ledger, referencial, relogio

GEOD = Geod(ellps="GRS80")
A = (-10.0, -50.0)
_lon, _lat, _ = GEOD.fwd(A[1], A[0], 45.0, 2000.0)
B = (_lat, _lon)
ORIGEM = datetime(2030, 1, 1, tzinfo=timezone.utc)


def corpo_ref(b=B, extra=None):
    c = {
        "datum": "SIRGAS2000",
        "origem_local": "THR_A",
        "referentes": {
            "THR_A": {"lat": A[0], "lon": A[1], "h": 500.0},
            "THR_B": {"lat": b[0], "lon": b[1], "h": 510.0},
        },
        "elementos_lineares": {"RWY_A_B": ["THR_A", "THR_B"]},
    }
    if extra:
        c["referentes"].update(extra)
    return c


def seed(ref, s=0.0, o=0.0, elemento="RWY_A_B"):
    return {"elemento": elemento, "referente": ref, "s": s, "o": o}


def montar(conn, com_swy=True, com_cabeceiras=True):
    """Cria run, airport com referencial e a pista (forma B). Devolve run_id.

    com_cabeceiras: registra as duas cabeceiras com as declarações de que as
    distâncias dependem (deslocamento_m, tem_swy, tem_cwy). Sem elas, a regra
    recusa TODA, ASDA e LDA.
    """
    run = relogio.novo_run(conn, ORIGEM)
    ref_id = referencial.publicar(conn, corpo_ref())
    ledger.append_event(conn, run, "XXXX", "airport", "BASELINE", asserted_by="teste",
                        corpo={"nome": "Aeródromo fictício",
                               "referencial": {"ref_id": ref_id},
                               "norma": {"variacao_magnetica_graus": -20}})
    relogio.avancar(conn, run, dias=1)
    ledger.append_event(conn, run, "XXXX-RWY", "runway", "BASELINE", asserted_by="teste",
                        corpo={"adc": {"designacao": "05/23", "direcoes": {"inicio": "05", "fim": "23"}},
                               "geometria": {"primitivos": {"pista": {
                                   "tipo": "retangulo", "inicio": seed("THR_A"), "fim": seed("THR_B"),
                                   "largura": 45}}}})
    if com_swy:
        ledger.append_event(conn, run, "XXXX-SWY", "protect_area", "BASELINE", asserted_by="teste",
                            corpo={"adc": {"papel": "swy", "aplica_a": {"pista": "XXXX-RWY", "direcao": "05"}},
                                   "geometria": {"primitivos": {"swy": {
                                       "tipo": "retangulo", "inicio": seed("THR_B"), "fim": seed("THR_B", s=-200),
                                       "largura": 45}}}})
    if com_cabeceiras:
        for fid, desig, swy in (("XXXX-THR05", "05", com_swy), ("XXXX-THR23", "23", False)):
            ledger.append_event(conn, run, fid, "runway_direction", "BASELINE", asserted_by="teste",
                                corpo={"adc": {"designador": desig, "pista": "XXXX-RWY", "deslocamento_m": 0,
                                               "tem_swy": swy, "tem_cwy": False}})
    return run
