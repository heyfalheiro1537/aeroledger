"""Projeção: seleção × camadas × instante → estados, com cache por marca d'água.

A projeção lê só event e event_field (e o referencial, quando o consumidor gera
geometria). O cache é invalidado pela marca d'água da própria projeção: o maior
event_id, até t, entre os eventos dos tipos selecionados. Um evento fora da
seleção não move a marca e não derruba o cache.
"""
from psycopg.types.json import Jsonb

from . import ledger
from .canonico import sha256


def chave(tipos, camadas):
    return sha256({"tipos": sorted(tipos) if tipos else None, "camadas": sorted(camadas) if camadas else None})


def marca(conn, run_id, tipos, t):
    with conn.cursor() as cur:
        cur.execute("""SELECT max(event_id) FROM event WHERE run_id = %s AND event_id <= %s
                       AND (%s::text[] IS NULL OR feature_type = ANY (%s::text[]))""",
                    (run_id, t, tipos, tipos))
        return cur.fetchone()[0]


def projetar(conn, run_id, t=None, tipos=None, camadas=None, usar_cache=True):
    """Devolve ({feature_id: {'tipo', 'estado'}}, info) com info = {'hwm', 'cache'}."""
    t_lim = ledger._limite(conn, run_id, t)
    hwm = marca(conn, run_id, tipos, t_lim)
    k = chave(tipos, camadas)
    if usar_cache and hwm is not None:
        with conn.cursor() as cur:
            cur.execute("SELECT conteudo FROM projecao_cache WHERE run_id = %s AND chave = %s AND hwm = %s",
                        (run_id, k, hwm))
            r = cur.fetchone()
        if r:
            return r[0], {"hwm": hwm, "cache": "hit"}
    conteudo = {}
    for fid, ft in ledger.features(conn, run_id, t_lim):
        if tipos and ft not in tipos:
            continue
        if camadas:
            estado = {}
            for c in camadas:
                parte = ledger.reconstruct_state(conn, run_id, fid, t_lim, prefixo=c + ".") or {}
                estado.update(parte)
        else:
            estado = ledger.reconstruct_state(conn, run_id, fid, t_lim)
        conteudo[fid] = {"tipo": ft, "estado": estado}
    if usar_cache and hwm is not None:
        with conn.cursor() as cur:
            cur.execute("""INSERT INTO projecao_cache (run_id, chave, hwm, conteudo) VALUES (%s,%s,%s,%s)
                           ON CONFLICT DO NOTHING""", (run_id, k, hwm, Jsonb(conteudo)))
        conn.commit()
    return conteudo, {"hwm": hwm, "cache": "miss"}
