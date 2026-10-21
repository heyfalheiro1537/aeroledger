"""Serialização canônica JSON (JCS, RFC 8785) e hash SHA-256.

O hash da cadeia e o ref_id do referencial dependem de uma única forma de
serializar o mesmo conteúdo. Regras do RFC 8785 aplicadas:
  * chaves ordenadas pela sequência de unidades UTF-16;
  * sem espaços; strings com o escape mínimo do JSON;
  * números no formato do ECMAScript (1.0 → 1, 1e21 → 1e+21, 1e-7 → 1e-7);
  * NaN e infinito são recusados.
"""
import hashlib
import json
import math
from decimal import Decimal


def _numero(x):
    """Number.prototype.toString do ECMAScript, como o RFC 8785 exige.

    Usa os dígitos mais curtos que reproduzem o float (os mesmos do repr do
    Python) e decide entre notação decimal e exponencial pela posição do ponto.
    Inteiros de verdade (int) saem como estão.
    """
    if isinstance(x, bool):
        raise TypeError("bool não é número")
    if isinstance(x, int):
        return str(x)
    if math.isnan(x) or math.isinf(x):
        raise ValueError("NaN e infinito não têm forma canônica")
    if x == 0:
        return "0"
    sinal, digitos, expoente = Decimal(repr(x)).as_tuple()
    digitos = list(digitos)
    while len(digitos) > 1 and digitos[-1] == 0:
        digitos.pop()
        expoente += 1
    d = "".join(map(str, digitos))
    k = len(d)
    n = k + expoente                      # x = 0.d × 10^n
    s = "-" if sinal else ""
    if k <= n <= 21:
        return s + d + "0" * (n - k)
    if 0 < n <= 21:
        return s + d[:n] + "." + d[n:]
    if -6 < n <= 0:
        return s + "0." + "0" * (-n) + d
    e = n - 1
    return s + d[0] + ("." + d[1:] if k > 1 else "") + "e" + ("+" if e > 0 else "-") + str(abs(e))


def _serializar(v):
    if v is None:
        return "null"
    if v is True:
        return "true"
    if v is False:
        return "false"
    if isinstance(v, (int, float)):
        return _numero(v)
    if isinstance(v, str):
        return json.dumps(v, ensure_ascii=False)
    if isinstance(v, (list, tuple)):
        return "[" + ",".join(_serializar(x) for x in v) + "]"
    if isinstance(v, dict):
        for k in v:
            if not isinstance(k, str):
                raise TypeError("chave de objeto JSON precisa ser string")
        chaves = sorted(v, key=lambda k: k.encode("utf-16-be"))
        return "{" + ",".join(json.dumps(k, ensure_ascii=False) + ":" + _serializar(v[k]) for k in chaves) + "}"
    raise TypeError(f"tipo sem forma JSON: {type(v).__name__}")


def canonico(v) -> bytes:
    return _serializar(v).encode("utf-8")


def sha256(v) -> str:
    return hashlib.sha256(canonico(v)).hexdigest()
