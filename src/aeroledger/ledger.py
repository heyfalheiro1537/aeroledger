"""Ledger em que só se escreve: o único ponto de escrita e as leituras.

append_event é a única função que escreve eventos. Ela recusa violação de
forma (DELTA sem BASELINE, envelope alterado, path malformado, seed que aponta
para referente inexistente) e calcula prev_hash e hash. Divergência de
expectativa de domínio não é recusada: é registrada, para continuar visível.

Nenhuma variável Python guarda estado do aeródromo: todo estado sai de
reconstruct_state sobre o banco.
"""
import re
from datetime import datetime

from psycopg.types.json import Jsonb

from . import relogio
from .canonico import sha256

TIPOS = ("BASELINE", "DELTA", "VOID")
PATH_RE = re.compile(r"^[a-z0-9_]+(\.[a-z0-9_]+)*$")


class LedgerError(Exception):
    """Violação de forma: o evento não é escrito."""


class _Removido:
    def __repr__(self):
        return "REMOVIDO"


REMOVIDO = _Removido()      # valor de alteração que remove um campo (ausente ≠ null)


# ---------------------------------------------------------------- corpo

def achatar(corpo: dict, prefixo="") -> dict:
    """{'a': {'b': 1}} → {'a.b': 1}. Listas e escalares são folhas."""
    out = {}
    for k, v in corpo.items():
        p = f"{prefixo}.{k}" if prefixo else k
        if isinstance(v, dict) and v:
            out.update(achatar(v, p))
        else:
            out[p] = v
    return out


def aninhar(plano: dict) -> dict:
    out = {}
    for p in sorted(plano):
        no = out
        partes = p.split(".")
        for parte in partes[:-1]:
            no = no.setdefault(parte, {})
        no[partes[-1]] = plano[p]
    return out


def _checar_arvore(plano: dict):
    """Um path não pode ser folha e prefixo de outro ao mesmo tempo."""
    chaves = sorted(plano)
    for a, b in zip(chaves, chaves[1:]):
        if b.startswith(a + "."):
            raise LedgerError(f"'{a}' é folha e prefixo de '{b}' ao mesmo tempo")


# ---------------------------------------------------------------- hash

# Campos do envelope que só entram no hash quando presentes. Assim um evento
# sem grupo tem exatamente o mesmo hash que na versão 0.5.0.
OPCIONAIS = ("grupo_id", "grupo_n")


def conteudo_para_hash(env: dict, campos: list) -> dict:
    env = {k: v for k, v in env.items() if not (k in OPCIONAIS and v is None)}
    return {**env, "campos": sorted(campos, key=lambda c: c["path"])}


ENVELOPE = ("event_id", "run_id", "feature_id", "feature_type", "event_type",
            "asserted_by", "owner_ref", "reason", "caused_by", "prev_hash", "grupo_id", "grupo_n")


# ---------------------------------------------------------------- limites de tempo

def _limite(conn, run_id, t):
    """Limite de leitura: maior event_id visível em t, sem nunca cortar um grupo.

    t pode ser None (o relógio do run), um instante ou um event_id. Se o limite
    cai dentro de um grupo (o último evento até ele pertence a um grupo de que
    nem todos os eventos estão até o limite), a leitura recua para antes do
    grupo, como faz o consumidor do tópico. Só a leitura passa por aqui; a
    escrita usa relogio.limite diretamente.
    """
    if t is None:
        lim = relogio.limite(relogio.agora(conn, run_id))
    elif isinstance(t, datetime):
        lim = relogio.limite(t)
    else:
        lim = int(t)
    return _recuar_grupo(conn, run_id, lim)


def _recuar_grupo(conn, run_id, lim):
    with conn.cursor() as cur:
        cur.execute("""SELECT grupo_id, grupo_n FROM event WHERE run_id = %s AND event_id <= %s
                       ORDER BY event_id DESC LIMIT 1""", (run_id, lim))
        ult = cur.fetchone()
        if ult is None or ult[0] is None:
            return lim
        gid, gn = ult
        cur.execute("SELECT count(*) FROM event WHERE run_id = %s AND grupo_id = %s AND event_id <= %s",
                    (run_id, gid, lim))
        if cur.fetchone()[0] < gn:
            return gid - 1
    return lim


# ---------------------------------------------------------------- escrita

def append_event(conn, run_id, feature_id, feature_type, event_type, *, asserted_by,
                 corpo=None, alteracoes=None, reason=None, owner_ref=None, caused_by=None):
    """Escreve um evento. Devolve (event_id, hash)."""
    try:
        with conn.cursor() as cur:
            agora_, shard = _travar_run(cur, run_id)
            r = _escrever(conn, cur, run_id, shard, agora_, feature_id, feature_type, event_type,
                          asserted_by=asserted_by, corpo=corpo, alteracoes=alteracoes, reason=reason,
                          owner_ref=owner_ref, caused_by=caused_by)
    except BaseException:
        conn.rollback()
        raise
    conn.commit()
    return r


def append_group(conn, run_id, eventos):
    """Escreve vários eventos como uma mudança só. Devolve [(event_id, hash)].

    Cada item de 'eventos' é um dict com os argumentos de append_event
    (feature_id, feature_type, event_type, asserted_by, corpo, alteracoes, ...).
    Os eventos ficam contíguos na cadeia, no mesmo instante, e levam no
    envelope grupo_id (o event_id do primeiro) e grupo_n (quantos são); os dois
    entram no hash. Ou todos são escritos, ou nenhum. Quem lê o tópico só corta
    o histórico em fronteira de grupo.
    """
    eventos = list(eventos)
    if len(eventos) < 2:
        raise LedgerError("grupo precisa de pelo menos dois eventos; para um, use append_event")
    out = []
    try:
        with conn.cursor() as cur:
            agora_, shard = _travar_run(cur, run_id)
            gid = None
            for ev in eventos:
                ev = dict(ev)
                fid, ft, et = ev.pop("feature_id"), ev.pop("feature_type"), ev.pop("event_type")
                eid, h = _escrever(conn, cur, run_id, shard, agora_, fid, ft, et,
                                   grupo=(gid, len(eventos)), **ev)
                gid = gid or eid
                out.append((eid, h))
    except BaseException:
        conn.rollback()
        raise
    conn.commit()
    return out


def _travar_run(cur, run_id):
    cur.execute("SELECT shard, relogio FROM run WHERE run_id = %s FOR UPDATE", (run_id,))
    linha = cur.fetchone()
    if linha is None:
        raise LedgerError(f"run inexistente: {run_id}")
    shard, agora_ = linha
    return agora_, shard


def _escrever(conn, cur, run_id, shard, agora_, feature_id, feature_type, event_type, *, asserted_by,
              corpo=None, alteracoes=None, reason=None, owner_ref=None, caused_by=None, grupo=None):
    """Valida e insere um evento dentro da transação aberta (sem commit)."""
    if event_type not in TIPOS:
        raise LedgerError(f"tipo de evento inválido: {event_type}")
    if not asserted_by:
        raise LedgerError("asserted_by é obrigatório")
    if event_type == "BASELINE":
        if corpo is None or alteracoes is not None:
            raise LedgerError("BASELINE leva corpo completo, não alterações")
        novos = {p: v for p, v in achatar(corpo).items()}
        if any(v is REMOVIDO for v in novos.values()):
            raise LedgerError("BASELINE não remove campos")
    elif event_type == "DELTA":
        if not alteracoes or corpo is not None:
            raise LedgerError("DELTA leva alterações não vazias, não corpo")
        novos = dict(alteracoes)
    else:
        if corpo is not None or alteracoes is not None:
            raise LedgerError("VOID não tem corpo")
        if not reason:
            raise LedgerError("VOID exige motivo")
        novos = {}
    for p in novos:
        if not PATH_RE.match(p):
            raise LedgerError(f"path malformado: {p!r}")

    # envelope imutável
    cur.execute("""SELECT feature_type, owner_ref FROM event
                   WHERE run_id = %s AND feature_id = %s ORDER BY event_id LIMIT 1""", (run_id, feature_id))
    primeiro = cur.fetchone()
    if primeiro and (primeiro[0] != feature_type or primeiro[1] != owner_ref):
        raise LedgerError("envelope imutável: feature_type e owner_ref não mudam; crie outra feature")

    # ciclo de vida
    t_lim = relogio.limite(agora_)
    cur.execute("SELECT viva_em(%s, %s, %s)", (run_id, feature_id, t_lim))
    viva = cur.fetchone()[0]
    if event_type == "BASELINE" and viva:
        raise LedgerError("BASELINE sobre feature viva; use DELTA, ou VOID antes")
    if event_type in ("DELTA", "VOID") and not viva:
        raise LedgerError(f"{event_type} sem BASELINE vigente")

    # estado resultante (forma da árvore e seeds)
    atual = {}
    if event_type == "DELTA":
        cur.execute("SELECT path, valor FROM estado_em(%s, %s, %s)", (run_id, feature_id, t_lim))
        atual = dict(cur.fetchall())
        for p, v in novos.items():
            if v is REMOVIDO and p not in atual:
                raise LedgerError(f"remoção de campo ausente: {p}")
    resultante = {**atual, **novos}
    resultante = {p: v for p, v in resultante.items() if v is not REMOVIDO}
    _checar_arvore(resultante)
    if event_type != "VOID":
        from .referencial import validar_escrita
        validar_escrita(conn, run_id, feature_id, feature_type, resultante, t_lim)

    # causa
    if caused_by is not None:
        cur.execute("SELECT 1 FROM event WHERE run_id = %s AND event_id = %s", (run_id, caused_by))
        if cur.fetchone() is None:
            raise LedgerError("caused_by aponta para evento inexistente neste run")

    # identificador e encadeamento
    cur.execute("SELECT event_id, hash FROM event WHERE run_id = %s ORDER BY event_id DESC LIMIT 1", (run_id,))
    ult = cur.fetchone()
    ms_ = relogio.ms(agora_)
    seq = 0
    if ult:
        ult_ms = ult[0] >> (relogio.BITS_SHARD + relogio.BITS_SEQ)
        if ult_ms > ms_:
            raise LedgerError("relógio atrás do último evento")
        if ult_ms == ms_:
            seq = (ult[0] & ((1 << relogio.BITS_SEQ) - 1)) + 1
    eid = relogio.compor(ms_, shard, seq)
    prev_hash = ult[1] if ult else None
    grupo_id, grupo_n = (None, None) if grupo is None else (grupo[0] or eid, grupo[1])

    campos = [{"path": p, "op": "removed" if v is REMOVIDO else "set",
               "next": None if v is REMOVIDO else v} for p, v in novos.items()]
    env = {"event_id": eid, "run_id": run_id, "feature_id": feature_id, "feature_type": feature_type,
           "event_type": event_type, "asserted_by": asserted_by, "owner_ref": owner_ref,
           "reason": reason, "caused_by": caused_by, "prev_hash": prev_hash,
           "grupo_id": grupo_id, "grupo_n": grupo_n}
    h = sha256(conteudo_para_hash(env, campos))

    cur.execute("""INSERT INTO event (event_id, run_id, feature_id, feature_type, event_type, asserted_by,
                                      owner_ref, reason, caused_by, prev_hash, grupo_id, grupo_n, hash)
                   VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
                (eid, run_id, feature_id, feature_type, event_type, asserted_by,
                 owner_ref, reason, caused_by, prev_hash, grupo_id, grupo_n, h))
    for c in campos:
        cur.execute("""INSERT INTO event_field (event_id, run_id, feature_id, path, op, next)
                       VALUES (%s,%s,%s,%s,%s,%s)""",
                    (eid, run_id, feature_id, c["path"], c["op"],
                     None if c["op"] == "removed" else Jsonb(c["next"])))
    return eid, h


# ---------------------------------------------------------------- leitura

def reconstruct_state(conn, run_id, feature_id, t=None, prefixo=None, usar_snapshot=False, plano=False):
    """Estado da feature ao fim de t (event_id, datetime ou None = agora).

    Devolve None se a feature não existe em t. Com prefixo, só a camada pedida
    ('geometria.'), sem ler as demais. Com usar_snapshot, parte do snapshot
    mais recente do mesmo ciclo e aplica só os eventos posteriores.
    """
    t = _limite(conn, run_id, t)
    with conn.cursor() as cur:
        cur.execute("SELECT viva_em(%s, %s, %s)", (run_id, feature_id, t))
        if not cur.fetchone()[0]:
            return None
        if usar_snapshot:
            estado = _com_snapshot(cur, run_id, feature_id, t, prefixo)
        else:
            cur.execute("SELECT path, valor FROM estado_em(%s, %s, %s, %s)", (run_id, feature_id, t, prefixo))
            estado = dict(cur.fetchall())
    return estado if plano else aninhar(estado)


def _baseline_vigente(cur, run_id, feature_id, t):
    cur.execute("""SELECT max(event_id) FROM event WHERE run_id = %s AND feature_id = %s
                   AND event_type = 'BASELINE' AND event_id <= %s""", (run_id, feature_id, t))
    return cur.fetchone()[0]


def _com_snapshot(cur, run_id, feature_id, t, prefixo):
    b = _baseline_vigente(cur, run_id, feature_id, t)
    cur.execute("""SELECT ate_event_id, estado FROM snapshot
                   WHERE run_id = %s AND feature_id = %s AND ciclo_baseline = %s AND ate_event_id <= %s
                   ORDER BY ate_event_id DESC LIMIT 1""", (run_id, feature_id, b, t))
    s = cur.fetchone()
    if s is None:
        desde, estado = b - 1, {}
    else:
        desde, estado = s[0], dict(s[1])
    cur.execute("""SELECT DISTINCT ON (path) path, op, next FROM event_field
                   WHERE run_id = %s AND feature_id = %s AND event_id > %s AND event_id <= %s
                   ORDER BY path, event_id DESC""", (run_id, feature_id, desde, t))
    for path, op, valor in cur.fetchall():
        if op == "removed":
            estado.pop(path, None)
        else:
            estado[path] = valor
    if prefixo:
        estado = {p: v for p, v in estado.items() if p.startswith(prefixo)}
    return estado


def gravar_snapshot(conn, run_id, feature_id, t=None):
    """Grava um snapshot (cache). Nunca é fonte de verdade."""
    t = _limite(conn, run_id, t)
    with conn.cursor() as cur:
        cur.execute("SELECT max(event_id) FROM event WHERE run_id = %s AND feature_id = %s AND event_id <= %s",
                    (run_id, feature_id, t))
        ate = cur.fetchone()[0]
        b = _baseline_vigente(cur, run_id, feature_id, t)
    estado = reconstruct_state(conn, run_id, feature_id, ate, plano=True)
    if estado is None:
        raise LedgerError("não há estado para guardar: feature inexistente em t")
    with conn.cursor() as cur:
        cur.execute("""INSERT INTO snapshot (run_id, feature_id, ate_event_id, ciclo_baseline, estado)
                       VALUES (%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING""",
                    (run_id, feature_id, ate, b, Jsonb(estado)))
    conn.commit()
    return ate


def features(conn, run_id, t=None, feature_type=None, tipos=None):
    """Features vivas em t, como [(feature_id, feature_type)].

    Lê por varredura saltada do índice (custo pelo nº de features, não de
    eventos), o que mantém a leitura independente do tamanho do histórico.
    """
    t = _limite(conn, run_id, t)
    if feature_type is not None:
        tipos = [feature_type]
    with conn.cursor() as cur:
        if tipos is None:
            cur.execute("SELECT feature_type FROM tipos_do_run(%s)", (run_id,))
            tipos = [r[0] for r in cur.fetchall()]
        vivas = []
        for tp in tipos:
            cur.execute("SELECT feature_id FROM features_de_tipo(%s, %s)", (run_id, tp))
            for (fid,) in cur.fetchall():
                cur.execute("SELECT viva_em(%s, %s, %s)", (run_id, fid, t))
                if cur.fetchone()[0]:
                    vivas.append((fid, tp))
    return sorted(vivas)


def ciclos(conn, run_id, feature_id):
    with conn.cursor() as cur:
        cur.execute("SELECT event_id, event_type, ciclo FROM ciclos(%s, %s)", (run_id, feature_id))
        return cur.fetchall()


AUSENTE = "<ausente>"


def diff(conn, run_id, t0, t1):
    """O que mudou entre t0 e t1: [(feature_id, path, antes, depois)]."""
    a, b = _limite(conn, run_id, t0), _limite(conn, run_id, t1)
    with conn.cursor() as cur:
        cur.execute("""SELECT DISTINCT feature_id FROM event WHERE run_id = %s AND event_id > %s AND event_id <= %s""",
                    (run_id, a, b))
        fids = [r[0] for r in cur.fetchall()]
    out = []
    for fid in sorted(fids):
        e0 = reconstruct_state(conn, run_id, fid, a, plano=True) or {}
        e1 = reconstruct_state(conn, run_id, fid, b, plano=True) or {}
        for p in sorted(set(e0) | set(e1)):
            v0, v1 = e0.get(p, AUSENTE), e1.get(p, AUSENTE)
            if v0 != v1:
                out.append((fid, p, v0, v1))
    return out


def marca_dagua(conn, run_id, feature_ids, t=None):
    t = _limite(conn, run_id, t)
    with conn.cursor() as cur:
        cur.execute("SELECT marca_dagua(%s, %s, %s)", (run_id, list(feature_ids), t))
        return cur.fetchone()[0]


def envelope(conn, event_id):
    with conn.cursor() as cur:
        cur.execute(f"SELECT {', '.join(ENVELOPE)}, hash FROM event WHERE event_id = %s", (event_id,))
        r = cur.fetchone()
    return dict(zip(ENVELOPE + ("hash",), r)) if r else None


def verify_chain(conn, run_id):
    """Recalcula o hash de cada evento e confere a ligação prev_hash.

    Devolve [primeiro event_id divergente], ou [] se a cadeia está íntegra.
    """
    with conn.cursor() as cur:
        cur.execute(f"SELECT {', '.join(ENVELOPE)}, hash FROM event WHERE run_id = %s ORDER BY event_id",
                    (run_id,))
        eventos = cur.fetchall()
        cur.execute("""SELECT event_id, path, op, next FROM event_field WHERE run_id = %s""", (run_id,))
        campos = {}
        for eid, path, op, nxt in cur.fetchall():
            campos.setdefault(eid, []).append({"path": path, "op": op, "next": nxt})
    anterior = None
    for linha in eventos:
        env = dict(zip(ENVELOPE, linha[:-1]))
        guardado = linha[-1]
        if env["prev_hash"] != anterior:
            return [env["event_id"]]
        if sha256(conteudo_para_hash(env, campos.get(env["event_id"], []))) != guardado:
            return [env["event_id"]]
        anterior = guardado
    return []


def cabeca(conn, run_id):
    """Hash do último evento: o que se registra fora do banco como âncora."""
    with conn.cursor() as cur:
        cur.execute("SELECT event_id, hash FROM event WHERE run_id = %s ORDER BY event_id DESC LIMIT 1", (run_id,))
        return cur.fetchone()
