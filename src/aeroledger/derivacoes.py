"""Conhecimento derivado: nunca armazenado, sempre calculado das premissas.

Comprimento e azimute vêm do referencial (geodésica entre as pontas do
elemento linear). Distâncias declaradas vêm da regra da norma aplicada ao
estado em t. Nada daqui é escrito no ledger.

Regra de distâncias declaradas (premissa normativa; Anexo 14 Vol. I, RBAC 154):
  TORA = comprimento da pista disponível para corrida
  TODA = TORA + CWY da direção
  ASDA = TORA + SWY da direção
  LDA  = comprimento da pista − deslocamento da cabeceira da direção
A regra com barreira de contenção ainda não está implementada;
com uma barreira viva, o cálculo recusa em vez de adivinhar.
"""
from pyproj import Geod

from . import ledger
from .referencial import Resolvedor, referencial_vigente

GEOD = Geod(ellps="GRS80")


class RegraPendente(Exception):
    pass


def comprimento(corpo_ref, elemento):
    res = Resolvedor(corpo_ref)
    a, b = res.pontas(elemento)
    pa, pb = res.referente(a), res.referente(b)
    _, _, d = GEOD.inv(pa[1], pa[0], pb[1], pb[0])
    return d


def azimutes(corpo_ref, elemento):
    """{ponta: azimute verdadeiro na ponta, apontando para a outra}."""
    res = Resolvedor(corpo_ref)
    a, b = res.pontas(elemento)
    return {a: res.azimute(elemento, a), b: res.azimute(elemento, b)}


def rumo_magnetico(az_verdadeiro, variacao_leste):
    """Rumo magnético; variação oeste é negativa (22°W → −22)."""
    return (az_verdadeiro - variacao_leste) % 360


def distancia_entre_seeds(corpo_ref, s1, s2):
    res = Resolvedor(corpo_ref)
    p, q = res.seed(s1), res.seed(s2)
    _, _, d = GEOD.inv(p[1], p[0], q[1], q[0])
    return d


CAMADAS_DERIVACAO = ["adc", "geometria"]


def distancias_de(corpo_ref, estados):
    """Regra das distâncias declaradas sobre estados já lidos: {fid: (tipo, estado)}.

    Pista ('runway'): retângulo 'pista' com inicio e fim nas cabeceiras e
      adc.direcoes = {'inicio': '16', 'fim': '34'} (a corrida da direção
      'inicio' parte do início do retângulo).
    SWY/CWY ('protect_area', adc.papel = 'swy' | 'cwy'): comprimento derivado
      das seeds; adc.aplica_a = {'pista': id, 'direcao': '16'}.
    Cabeceira ('runway_direction'): adc.designador, adc.pista e, quando houver,
      adc.deslocamento_m (premissa).
    """
    if any(tp == "arresting_gear" for tp, _ in estados.values()):
        raise RegraPendente("barreira de contenção viva: a regra de distâncias declaradas com barreira não está implementada")
    out = {}
    for fid, (tp, e) in estados.items():
        if tp != "runway":
            continue
        ret = e["geometria"]["primitivos"]["pista"]
        L = distancia_entre_seeds(corpo_ref, ret["inicio"], ret["fim"])
        for lado in ("inicio", "fim"):
            d = e["adc"]["direcoes"][lado]
            swy = cwy = desloc = 0.0
            for tp2, e2 in estados.values():
                a2 = e2.get("adc", {})
                if tp2 == "protect_area" and a2.get("aplica_a", {}).get("pista") == fid \
                        and a2["aplica_a"].get("direcao") == d and a2.get("papel") in ("swy", "cwy"):
                    r2 = next(iter(e2["geometria"]["primitivos"].values()))
                    comp = distancia_entre_seeds(corpo_ref, r2["inicio"], r2["fim"])
                    if a2["papel"] == "swy":
                        swy += comp
                    else:
                        cwy += comp
                if tp2 == "runway_direction" and a2.get("pista") == fid and a2.get("designador") == d:
                    desloc = float(a2.get("deslocamento_m", 0.0))
            out[d] = {"TORA": L, "TODA": L + cwy, "ASDA": L + swy, "LDA": L - desloc}
    return out


def ler_estados(conn, run_id, t, tipos, camadas):
    """{fid: (tipo, estado)} lendo só as camadas pedidas (contrato como lente)."""
    estados = {}
    for fid, tp in ledger.features(conn, run_id, t, tipos=tipos):
        e = {}
        for c in camadas:
            e.update(ledger.reconstruct_state(conn, run_id, fid, t, prefixo=c + ".") or {})
        estados[fid] = (tp, e)
    return estados


def distancias_declaradas(conn, run_id, t=None):
    """{designador: {TORA, TODA, ASDA, LDA}} para cada pista viva em t."""
    t_lim = ledger._limite(conn, run_id, t)
    _, corpo = referencial_vigente(conn, run_id, t_lim)
    if corpo is None:
        raise ValueError("sem referencial vigente")
    estados = ler_estados(conn, run_id, t_lim,
                          ["runway", "runway_direction", "protect_area", "arresting_gear"], CAMADAS_DERIVACAO)
    return distancias_de(corpo, estados)
