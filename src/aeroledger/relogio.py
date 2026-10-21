"""Relógio de simulação monotônico e identificadores snowflake.

Cada execução (run) declara um instante de origem uma vez. O relógio só
avança; não existe operação que o atrase. O event_id carrega o instante do
relógio de simulação, não o do computador: o simulador comprime o tempo sem
nunca escrever no passado.

event_id = ms desde 2000-01-01 UTC (41 bits) | shard do run (10 bits) | sequência (12 bits)
"""
import secrets
from datetime import datetime, timedelta, timezone

EPOCA = datetime(2000, 1, 1, tzinfo=timezone.utc)
BITS_SHARD, BITS_SEQ = 10, 12


class RelogioError(Exception):
    pass


def ms(t: datetime) -> int:
    return int((t - EPOCA) / timedelta(milliseconds=1))


def compor(ms_: int, shard: int, seq: int) -> int:
    if not (0 <= seq < 1 << BITS_SEQ):
        raise RelogioError("mais de 4096 eventos no mesmo milissegundo de simulação")
    return (ms_ << (BITS_SHARD + BITS_SEQ)) | (shard << BITS_SEQ) | seq


def instante(event_id: int) -> datetime:
    return EPOCA + timedelta(milliseconds=event_id >> (BITS_SHARD + BITS_SEQ))


def limite(t: datetime) -> int:
    """Maior event_id possível ao fim do instante t (resolve o 't ambíguo')."""
    return ((ms(t) + 1) << (BITS_SHARD + BITS_SEQ)) - 1


def novo_run(conn, origem: datetime, run_id: str | None = None) -> str:
    if origem.tzinfo is None:
        raise RelogioError("origem precisa de fuso horário")
    run_id = run_id or "run-" + secrets.token_hex(6)
    with conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM run")
        shard = cur.fetchone()[0] % (1 << BITS_SHARD)
        cur.execute("INSERT INTO run (run_id, shard, origem, relogio) VALUES (%s, %s, %s, %s)",
                    (run_id, shard, origem, origem))
    conn.commit()
    return run_id


def agora(conn, run_id: str) -> datetime:
    with conn.cursor() as cur:
        cur.execute("SELECT relogio FROM run WHERE run_id = %s", (run_id,))
        return cur.fetchone()[0]


def avancar(conn, run_id: str, dias=0, horas=0, minutos=0, segundos=0) -> datetime:
    passo = timedelta(days=dias, hours=horas, minutes=minutos, seconds=segundos)
    if passo < timedelta(0):
        raise RelogioError("o relógio só avança")
    with conn.cursor() as cur:
        cur.execute("UPDATE run SET relogio = relogio + %s WHERE run_id = %s RETURNING relogio", (passo, run_id))
        t = cur.fetchone()[0]
    conn.commit()
    return t
