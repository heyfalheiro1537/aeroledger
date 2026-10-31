"""Grupos na escrita, tópico geral e consumidores (aeródromo fictício)."""
import pytest

from aeroledger import derivacoes, ledger, referencial, relogio, topico
from aeroledger.canonico import sha256
from aeroledger.ledger import LedgerError
from cenario_sintetico import corpo_ref, montar, seed

T = topico


def carga_em_grupo(conn, run):
    """THR_A/THR_B e a SWY entram como uma mudança só."""
    return ledger.append_group(conn, run, [
        {"feature_id": "XXXX-THR05", "feature_type": "runway_direction", "event_type": "BASELINE",
         "asserted_by": "t", "corpo": {"adc": {"designador": "05", "pista": "XXXX-RWY", "deslocamento_m": 100.0}}},
        {"feature_id": "XXXX-THR23", "feature_type": "runway_direction", "event_type": "BASELINE",
         "asserted_by": "t", "corpo": {"adc": {"designador": "23", "pista": "XXXX-RWY"}}},
        {"feature_id": "XXXX-SWY23", "feature_type": "protect_area", "event_type": "BASELINE", "asserted_by": "t",
         "corpo": {"adc": {"papel": "swy", "aplica_a": {"pista": "XXXX-RWY", "direcao": "23"}},
                   "geometria": {"primitivos": {"swy": {"tipo": "retangulo", "inicio": seed("THR_A"),
                                                        "fim": seed("THR_A", s=-150), "largura": 45}}}}},
    ])


@pytest.fixture
def cenario(conn):
    run = montar(conn)
    relogio.avancar(conn, run, dias=1)
    grupo = carga_em_grupo(conn, run)
    relogio.avancar(conn, run, dias=1)
    ledger.append_event(conn, run, "INC-1", "incident", "BASELINE", asserted_by="t", owner_ref="XXXX-RWY",
                        corpo={"texto": "fod"})
    relogio.avancar(conn, run, dias=1)
    ledger.append_event(conn, run, "INC-1", "incident", "VOID", asserted_by="t", owner_ref="XXXX-RWY",
                        reason="removido")
    relogio.avancar(conn, run, dias=1)
    b = (corpo_ref()["referentes"]["THR_B"]["lat"] + 1e-5, corpo_ref()["referentes"]["THR_B"]["lon"])
    ref2 = referencial.publicar(conn, corpo_ref(b=b))
    ledger.append_event(conn, run, "XXXX", "airport", "DELTA", asserted_by="t",
                        alteracoes={"referencial.ref_id": ref2}, reason="remedição")
    return run, grupo


def ids(conn, run):
    with conn.cursor() as cur:
        cur.execute("SELECT event_id FROM event WHERE run_id = %s ORDER BY event_id", (run,))
        return [r[0] for r in cur.fetchall()]


# ---------------------------------------------------------------- grupos

def test_grupo_contiguo_no_mesmo_instante_e_no_hash(conn, cenario):
    run, grupo = cenario
    eids = [e for e, _ in grupo]
    todos = ids(conn, run)
    i = todos.index(eids[0])
    assert todos[i:i + 3] == eids                                     # contíguos
    assert len({relogio.instante(e) for e in eids}) == 1               # mesmo instante
    for e in eids:
        env = ledger.envelope(conn, e)
        assert (env["grupo_id"], env["grupo_n"]) == (eids[0], 3)
    assert ledger.verify_chain(conn, run) == []
    with conn.cursor() as cur:                                         # o grupo está dentro do hash
        cur.execute("UPDATE event SET grupo_n = 2 WHERE event_id = %s", (eids[1],))
    conn.commit()
    assert ledger.verify_chain(conn, run) == [eids[1]]


def test_evento_sem_grupo_tem_o_hash_da_versao_anterior(conn, cenario):
    run, _ = cenario
    env = ledger.envelope(conn, ids(conn, run)[0])
    antigo = {k: env[k] for k in ("event_id", "run_id", "feature_id", "feature_type", "event_type",
                                  "asserted_by", "owner_ref", "reason", "caused_by", "prev_hash")}
    with conn.cursor() as cur:
        cur.execute("SELECT path, op, next FROM event_field WHERE event_id = %s", (env["event_id"],))
        campos = [{"path": p, "op": o, "next": n} for p, o, n in cur.fetchall()]
    assert sha256({**antigo, "campos": sorted(campos, key=lambda c: c["path"])}) == env["hash"]


def test_grupo_e_tudo_ou_nada(conn):
    run = montar(conn)
    n = len(ids(conn, run))
    with pytest.raises(LedgerError):
        ledger.append_group(conn, run, [
            {"feature_id": "A", "feature_type": "taxiway", "event_type": "BASELINE", "asserted_by": "t",
             "corpo": {"a": 1}},
            {"feature_id": "A", "feature_type": "taxiway", "event_type": "BASELINE", "asserted_by": "t",
             "corpo": {"a": 2}},                                       # BASELINE sobre viva
        ])
    assert len(ids(conn, run)) == n
    with pytest.raises(LedgerError):
        ledger.append_group(conn, run, [{"feature_id": "A", "feature_type": "taxiway", "event_type": "BASELINE",
                                         "asserted_by": "t", "corpo": {"a": 1}}])


# ---------------------------------------------------------------- tópico

def test_retransmitir_e_idempotente_e_refazivel(conn, cenario):
    run, _ = cenario
    t1 = T.TopicoMemoria()
    n = T.retransmitir(conn, run, t1)
    assert n == len(ids(conn, run)) and T.retransmitir(conn, run, t1) == 0
    assert sum(m["tipo"] == "referencial" for m in t1.mensagens) == 2   # um por referencial adotado
    t2 = T.TopicoMemoria()
    T.retransmitir(conn, run, t2)
    assert t2.mensagens == t1.mensagens


# ---------------------------------------------------------------- consumidor

def consumidor_em_dia(conn, run, **kw):
    top = T.TopicoMemoria()
    T.retransmitir(conn, run, top)
    c = T.Consumidor(top, **kw)
    c.consumir()
    return top, c


def test_consumidor_bate_com_o_ledger_em_cada_fronteira(conn, cenario):
    run, grupo = cenario
    _, c = consumidor_em_dia(conn, run)
    dentro_do_grupo = {e for e, _ in grupo[:-1]}
    for n in ids(conn, run):
        if n in dentro_do_grupo:
            continue
        r = c.ler(n)
        assert r["ate"] == n
        for fid, tp in ledger.features(conn, run, n):
            assert r["estados"][fid] == {"tipo": tp, "estado": ledger.reconstruct_state(conn, run, fid, n)}
        assert set(r["estados"]) == {f for f, _ in ledger.features(conn, run, n)}
        try:
            esperado = derivacoes.distancias_declaradas(conn, run, n)
        except derivacoes.RegraPendente as exc:
            esperado = {"recusado": str(exc)}
        assert r["derivados"]["distancias"] == esperado


def test_corte_no_meio_do_grupo_cai_na_fronteira(conn, cenario):
    run, grupo = cenario
    _, c = consumidor_em_dia(conn, run)
    antes = ids(conn, run)[ids(conn, run).index(grupo[0][0]) - 1]
    for e, _ in grupo[:-1]:
        assert c.ler(e)["ate"] == antes
    # o ledger, cortado no mesmo ponto, mostraria a mudança pela metade
    assert ledger.reconstruct_state(conn, run, "XXXX-THR05", grupo[0][0]) is not None
    assert ledger.reconstruct_state(conn, run, "XXXX-SWY23", grupo[0][0]) is None
    assert "XXXX-THR05" not in c.ler(grupo[0][0])["estados"]


def test_cadencias_diferentes_convergem(conn, cenario):
    run, _ = cenario
    top, inteiro = consumidor_em_dia(conn, run)
    n = inteiro.cabeca[0]
    for passo in (1, 2, 3, 5):                                          # 1 e 2: menores que o grupo
        c = T.Consumidor(top)
        while not c.em_dia():
            antes = c.offset
            c.consumir(passo)
            assert c.offset > antes                                    # sempre avança
        assert c.ler(n)["hash"] == inteiro.ler(n)["hash"]


def test_retomada_depois_de_queda(conn, cenario, tmp_path):
    run, _ = cenario
    top, inteiro = consumidor_em_dia(conn, run)
    c = T.Consumidor(top)
    c.consumir(4)                                                      # para no meio do grupo
    arq = c.salvar(tmp_path / "c.json")
    del c
    c2 = T.Consumidor.carregar(arq, top)
    c2.consumir()
    n = inteiro.cabeca[0]
    assert c2.ler(n)["hash"] == inteiro.ler(n)["hash"]


def test_subconjunto_chega_aos_mesmos_derivados(conn, cenario):
    run, _ = cenario
    top, inteiro = consumidor_em_dia(conn, run)
    sub = T.Consumidor(top, raizes=["XXXX-THR05"])
    sub.consumir()
    n = inteiro.cabeca[0]
    r, rs = inteiro.ler(n), sub.ler(n)
    assert rs["hash_derivados"] == r["hash_derivados"]
    assert set(rs["fecho"]) == {"XXXX", "XXXX-RWY", "XXXX-THR05", "XXXX-THR23", "XXXX-SWY", "XXXX-SWY23"}
    assert all(rs["estados"][f] == r["estados"][f] for f in rs["fecho"])


def test_atrasado_recusa_em_vez_de_responder_velho(conn, cenario):
    run, _ = cenario
    top = T.TopicoMemoria()
    T.retransmitir(conn, run, top)
    c = T.Consumidor(top)
    c.consumir(2)
    with pytest.raises(T.ConsumidorAtrasado):
        c.ler(ids(conn, run)[-1])


@pytest.mark.parametrize("ataque", ["remover", "alterar", "alterar_e_rehash", "inserir"])
def test_canal_adulterado_e_detectado(conn, cenario, ataque):
    run, _ = cenario
    top = T.TopicoMemoria()
    T.retransmitir(conn, run, top)
    alvo = next(i for i, m in enumerate(top.mensagens) if m["tipo"] == "evento" and m["evento"]["feature_id"] == "INC-1")
    ev = top.mensagens[alvo]["evento"]
    if ataque == "remover":
        del top.mensagens[alvo]
    elif ataque in ("alterar", "alterar_e_rehash"):
        ev["campos"][0]["next"] = "adulterado"
        if ataque == "alterar_e_rehash":
            ev["hash"] = T.hash_do_evento(ev)
    else:
        falso = dict(ev, feature_id="FALSO", event_id=ev["event_id"] + 1, prev_hash=ev["hash"])
        falso["hash"] = T.hash_do_evento(falso)
        top.mensagens.insert(alvo + 1, {"tipo": "evento", "evento": falso})
    c = T.Consumidor(top)
    with pytest.raises(T.TopicoCorrompido) as exc:
        c.consumir()
    esperado = {"remover": alvo, "alterar": alvo, "alterar_e_rehash": alvo + 1, "inserir": alvo + 2}[ataque]
    assert exc.value.offset == esperado
    # nada falso ficou aplicado: o consumidor está num prefixo que não depende da mensagem duvidosa
    assert c.cabeca[0] <= ev["event_id"]
    assert not any(e["feature_id"] == "FALSO" or any(f["next"] == "adulterado" for f in e["campos"])
                   for e in c.mensagens)
    assert ledger.verify_chain(conn, run) == []                        # o ledger não foi tocado


def test_entrega_repetida_e_ignorada(conn, cenario):
    run, _ = cenario
    top, inteiro = consumidor_em_dia(conn, run)
    dup = T.TopicoMemoria()
    for m in top.mensagens[:6] + top.mensagens[2:]:
        dup.anexar(m)
    c = T.Consumidor(dup)
    c.consumir()
    n = inteiro.cabeca[0]
    assert c.ler(n)["hash"] == inteiro.ler(n)["hash"]


def test_referencial_adulterado_e_ausente(conn, cenario):
    run, _ = cenario
    top = T.TopicoMemoria()
    T.retransmitir(conn, run, top)
    i = next(i for i, m in enumerate(top.mensagens) if m["tipo"] == "referencial")
    top.mensagens[i]["corpo"]["datum"] = "WGS84"
    with pytest.raises(T.TopicoCorrompido):
        T.Consumidor(top).consumir()
    del top.mensagens[i]
    c = T.Consumidor(top)
    c.consumir()
    assert c.ler(ids(conn, run)[0])["derivados"] == {"recusado": "corpo do referencial não recebido"}


def test_leitura_com_ledger_fora(conn, cenario):
    run, _ = cenario
    _, c = consumidor_em_dia(conn, run)
    n = c.cabeca[0]
    antes = c.ler(n)
    conn.close()
    assert c.ler(n) == antes


def test_grupo_que_ainda_nao_chegou_inteiro_fica_pendente(conn, cenario):
    run, grupo = cenario
    top = T.TopicoMemoria()
    T.retransmitir(conn, run, top)
    i = next(i for i, m in enumerate(top.mensagens) if m["tipo"] == "evento" and m["evento"]["event_id"] == grupo[1][0])
    parcial = T.TopicoMemoria()
    for m in top.mensagens[:i + 1]:                                    # o broker só tem 2 dos 3
        parcial.anexar(m)
    c = T.Consumidor(parcial)
    c.consumir()
    assert c.cabeca[0] < grupo[0][0] and not c.em_dia()
    for m in top.mensagens[i + 1:]:                                    # chega o resto
        parcial.anexar(m)
    c.consumir()
    assert c.cabeca[0] == ids(conn, run)[-1]
