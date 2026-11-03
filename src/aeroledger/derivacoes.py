"""Conhecimento derivado: nunca armazenado, sempre calculado das premissas.

Comprimento e azimute vêm do referencial (geodésica entre as pontas do
elemento linear). Distâncias declaradas vêm da regra da norma aplicada ao
estado em t. Nada daqui é escrito no ledger.

Regra de distâncias declaradas (premissa normativa; Anexo 14 Vol. I, RBAC 154):
  TORA = comprimento da pista (geodésica entre as extremidades do retângulo)
  TODA = TORA + CWY da direção
  ASDA = TORA + SWY da direção
  LDA  = comprimento da pista − deslocamento da cabeceira da direção

Ausente não é zero. Cada direção (runway_direction) declara, na camada adc:
  deslocamento_m  número ≥ 0; 0 afirma que a cabeceira coincide com a extremidade
  tem_swy         true ou false: se há zona de parada para a decolagem nessa direção
  tem_cwy         true ou false: idem para a zona livre de obstáculos
Sem a declaração, o valor que depende dela é recusado, e não calculado com zero.
Uma declaração que contradiz as features (tem_swy true sem SWY registrada, ou
false com SWY registrada) também leva à recusa. A recusa vem por valor, em
distancias[designador]["recusado"] = {grandeza: motivo}; os valores calculáveis
continuam presentes.

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

    Pista ('runway'): retângulo 'pista' com inicio e fim nas extremidades e
      adc.direcoes = {'inicio': '16', 'fim': '34'} (a corrida da direção
      'inicio' parte do início do retângulo).
    SWY/CWY ('protect_area', adc.papel = 'swy' | 'cwy'): comprimento derivado
      das seeds; adc.aplica_a = {'pista': id, 'direcao': '16'}.
    Cabeceira ('runway_direction'): adc.designador, adc.pista e as declarações
      deslocamento_m, tem_swy e tem_cwy (ver o cabeçalho do módulo).
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
            areas = {"swy": [], "cwy": []}
            cabeceiras = []
            for tp2, e2 in estados.values():
                a2 = e2.get("adc", {})
                if tp2 == "protect_area" and a2.get("aplica_a", {}).get("pista") == fid \
                        and a2["aplica_a"].get("direcao") == d and a2.get("papel") in areas:
                    r2 = next(iter(e2["geometria"]["primitivos"].values()))
                    areas[a2["papel"]].append(distancia_entre_seeds(corpo_ref, r2["inicio"], r2["fim"]))
                if tp2 == "runway_direction" and a2.get("pista") == fid and a2.get("designador") == d:
                    cabeceiras.append(a2)
            valores = {"TORA": L}
            recusas = {}
            if len(cabeceiras) != 1:
                motivo = (f"cabeceira {d} não registrada" if not cabeceiras
                          else f"mais de uma cabeceira {d} registrada")
                recusas.update({k: motivo for k in ("TODA", "ASDA", "LDA")})
            else:
                cab = cabeceiras[0]
                for grandeza, papel, campo, nome in (("TODA", "cwy", "tem_cwy", "zona livre de obstáculos"),
                                                    ("ASDA", "swy", "tem_swy", "zona de parada")):
                    declarado = cab.get(campo)
                    achadas = areas[papel]
                    if declarado is None:
                        recusas[grandeza] = f"{campo} não declarado na cabeceira {d}"
                    elif declarado is True and not achadas:
                        recusas[grandeza] = f"{nome} declarada na cabeceira {d}, mas não registrada"
                    elif declarado is False and achadas:
                        recusas[grandeza] = f"{nome} registrada, mas a cabeceira {d} declara que não há"
                    elif declarado not in (True, False):
                        recusas[grandeza] = f"{campo} da cabeceira {d} precisa ser true ou false"
                    else:
                        valores[grandeza] = L + sum(achadas)
                desloc = cab.get("deslocamento_m")
                if desloc is None:
                    recusas["LDA"] = f"deslocamento_m não informado na cabeceira {d}"
                elif isinstance(desloc, bool) or not isinstance(desloc, (int, float)) or desloc < 0:
                    recusas["LDA"] = f"deslocamento_m da cabeceira {d} precisa ser um número ≥ 0"
                else:
                    valores["LDA"] = L - float(desloc)
            if recusas:
                valores["recusado"] = recusas
            out[d] = valores
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
