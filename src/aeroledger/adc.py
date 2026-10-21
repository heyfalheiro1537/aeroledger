"""Lente ADC: o que uma carta de aeródromo precisa, lido do ledger em t.

O contrato do ADC lê só duas camadas de cada feature ('adc' e 'geometria') e só
os tipos que a carta mostra. Tudo o mais que o ledger guarde (manutenção,
inspeções, incidentes) fica de fora sem ser lido: é o contrato como lente.

Nada é armazenado: a geometria sai do motor, a tabela de distâncias sai da
regra da norma, ambas no referencial vigente em t.
"""
from . import ledger, motor
from .derivacoes import distancias_de, ler_estados, RegraPendente
from .referencial import referencial_vigente

TIPOS_ADC = ["runway", "runway_direction", "protect_area", "taxiway", "apron", "pavement",
             "navaid", "aerodrome_point", "arresting_gear", "restriction"]
CAMADAS_ADC = ["adc", "geometria"]


def projetar(conn, run_id, t=None, n_segmentos=16, usar_cache=False):
    """Conteúdo do ADC em t: elementos com geometria, nós e tabela de distâncias.

    usar_cache: lê os estados pela projeção com cache por marca d'água
    (aeroledger.projecao); o resultado tem de ser idêntico ao da leitura direta.
    """
    t_lim = ledger._limite(conn, run_id, t)
    ref_id, corpo = referencial_vigente(conn, run_id, t_lim)
    if corpo is None:
        raise ValueError("sem referencial vigente em t")
    if usar_cache:
        from . import projecao
        conteudo, _ = projecao.projetar(conn, run_id, t_lim, tipos=TIPOS_ADC, camadas=CAMADAS_ADC, usar_cache=True)
        estados = {fid: (v["tipo"], v["estado"]) for fid, v in conteudo.items()}
    else:
        estados = ler_estados(conn, run_id, t_lim, TIPOS_ADC, CAMADAS_ADC)
    elementos = []
    for fid in sorted(estados):
        tp, e = estados[fid]
        forma, nos = (None, [])
        if "geometria" in e:
            forma, nos = motor.gerar(e["geometria"], corpo, n_segmentos=n_segmentos)
        elementos.append({"feature_id": fid, "tipo": tp, "adc": e.get("adc", {}), "forma": forma, "nos": nos,
                          "n_primitivos": motor.contar_primitivos(e.get("geometria", {}))})
    try:
        tabela = distancias_de(corpo, estados)
    except RegraPendente as exc:
        tabela = {"pendente": str(exc)}
    return {"t": t_lim, "ref_id": ref_id, "referencial": corpo, "elementos": elementos, "distancias": tabela,
            "estados": estados}


def conteudo_canonico(adc):
    """Forma comparável do ADC (para conferir cache × leitura direta): estados, geometria em WKT e tabela."""
    return {"ref_id": adc["ref_id"], "distancias": adc["distancias"],
            "elementos": [{"feature_id": e["feature_id"], "tipo": e["tipo"], "adc": e["adc"],
                           "forma": e["forma"].wkt if e["forma"] is not None else None,
                           "nos": [p.wkt for p in e["nos"]]} for e in adc["elementos"]]}
