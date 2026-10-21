"""Referencial linear (ISO 19148) na forma B, com referentes múltiplos.

Corpo de um referencial:

    {
      "datum": "SIRGAS2000",
      "origem_local": "THR16",                     # origem do plano ENU do motor
      "referentes": {
        "THR16": {"lat": -23.2, "lon": -45.8, "h": 616.7},        # georreferenciado (premissa)
        "THR34": {"lat": -23.2, "lon": -45.8, "h": 644.0},
        "THR34D": {"elemento": "RWY_16_34", "de": "THR34", "s": 300, "o": 0}   # relativo
      },
      "elementos_lineares": {"RWY_16_34": ["THR16", "THR34"]}
    }

Regras (recusadas na publicação):
  1. um referente é georreferenciado (lat, lon) ou relativo (elemento, de, s, o), nunca os dois;
  2. os referentes formam um grafo sem ciclos (as raízes são os georreferenciados);
  3. um ponto sozinho não define direção: um referente relativo é medido ao
     longo de um elemento linear, a partir de uma das pontas dele.

Seed (posição de qualquer elemento): {"elemento", "referente", "s", "o"}.
  s: distância geodésica ao longo do eixo, a partir do referente, positiva no
     sentido da ponta oposta (para referente relativo: no sentido que se afasta
     do seu "de");
  o: afastamento lateral geodésico, positivo à direita de quem anda no sentido de s.

O ref_id é o SHA-256 do corpo em JCS. Publicar um referencial grava o corpo
(imutável) e um DELTA no airport com o ref_id novo; as seeds não mudam.
"""
import re

from psycopg.types.json import Jsonb
from pyproj import Geod

from .canonico import sha256

GEOD = Geod(ellps="GRS80")
ID_RE = re.compile(r"^[A-Za-z0-9_\-]+$")


class ReferencialError(Exception):
    pass


# ---------------------------------------------------------------- validação

def _tipo_referente(r: dict) -> str:
    geo = {"lat", "lon"} <= set(r)
    rel = {"elemento", "de", "s", "o"} <= set(r)
    if geo and rel:
        raise ReferencialError("referente georreferenciado e relativo ao mesmo tempo (regra 1)")
    if geo:
        return "geo"
    if rel:
        return "rel"
    if "s" in r or "de" in r:
        raise ReferencialError("referente relativo precisa de elemento, de, s e o (regra 3)")
    raise ReferencialError("referente sem posição: precisa de (lat, lon) ou (elemento, de, s, o)")


def validar_corpo(corpo: dict):
    refs = corpo.get("referentes") or {}
    elems = corpo.get("elementos_lineares") or {}
    if not refs:
        raise ReferencialError("referencial sem referentes")
    for nome in list(refs) + list(elems):
        if not ID_RE.match(nome):
            raise ReferencialError(f"identificador inválido: {nome!r}")
    for nome, pontas in elems.items():
        if len(pontas) != 2 or pontas[0] == pontas[1]:
            raise ReferencialError(f"elemento {nome}: precisa de dois referentes distintos")
        for p in pontas:
            if p not in refs:
                raise ReferencialError(f"elemento {nome}: referente inexistente {p}")
    deps = {}
    for nome, r in refs.items():
        if _tipo_referente(r) == "rel":
            e = elems.get(r["elemento"])
            if e is None:
                raise ReferencialError(f"{nome}: elemento inexistente {r['elemento']}")
            if r["de"] not in e:
                raise ReferencialError(f"{nome}: 'de' precisa ser uma ponta do elemento (regra 3)")
            deps[nome] = set(e)
        else:
            deps[nome] = set()
    # regra 2: sem ciclos
    estado = {}

    def visitar(n, pilha):
        if estado.get(n) == 1:
            raise ReferencialError(f"ciclo entre referentes: {' → '.join(pilha + [n])} (regra 2)")
        if estado.get(n) == 2:
            return
        estado[n] = 1
        for d in deps[n]:
            visitar(d, pilha + [n])
        estado[n] = 2
    for n in refs:
        visitar(n, [])
    if corpo.get("origem_local") not in refs:
        raise ReferencialError("origem_local precisa ser um referente")


def publicar(conn, corpo: dict) -> str:
    """Valida e grava o corpo; devolve o ref_id. Não escreve evento."""
    validar_corpo(corpo)
    ref_id = sha256(corpo)
    with conn.cursor() as cur:
        cur.execute("INSERT INTO referencial (ref_id, corpo) VALUES (%s, %s) ON CONFLICT DO NOTHING",
                    (ref_id, Jsonb(corpo)))
    conn.commit()
    return ref_id


def carregar(conn, ref_id: str) -> dict:
    with conn.cursor() as cur:
        cur.execute("SELECT corpo FROM referencial WHERE ref_id = %s", (ref_id,))
        r = cur.fetchone()
    if r is None:
        raise ReferencialError(f"referencial inexistente: {ref_id}")
    return r[0]


def verificar_referenciais(conn):
    """ref_id que não confere com o hash do próprio corpo (adulteração)."""
    with conn.cursor() as cur:
        cur.execute("SELECT ref_id, corpo FROM referencial")
        return [rid for rid, corpo in cur.fetchall() if sha256(corpo) != rid]


# ---------------------------------------------------------------- resolução

class Resolvedor:
    """Resolve referentes e seeds de um corpo de referencial em (lat, lon)."""

    def __init__(self, corpo: dict):
        validar_corpo(corpo)
        self.corpo = corpo
        self._memo = {}

    def pontas(self, elemento):
        e = self.corpo["elementos_lineares"].get(elemento)
        if e is None:
            raise ReferencialError(f"elemento inexistente: {elemento}")
        return e

    def referente(self, nome):
        """(lat, lon) de um referente."""
        return self._ponto_e_azimute(nome)[:2]

    def _ponto_e_azimute(self, nome):
        """(lat, lon, az) — az é o azimute do eixo no ponto, no sentido positivo de s
        (None para referente georreferenciado, cujo sentido depende do elemento)."""
        if nome in self._memo:
            return self._memo[nome]
        r = self.corpo["referentes"].get(nome)
        if r is None:
            raise ReferencialError(f"referente inexistente: {nome}")
        if _tipo_referente(r) == "geo":
            v = (float(r["lat"]), float(r["lon"]), None)
        else:
            de = r["de"]
            lat, lon = self.referente(de)
            v = self._andar(lat, lon, self.azimute(r["elemento"], de), float(r["s"]), float(r["o"]))
        self._memo[nome] = v
        return v

    def sentido(self, elemento, referente):
        """Ponta de onde parte o sentido positivo de s, para este referente."""
        pontas = self.pontas(elemento)
        if referente in pontas:
            return referente
        r = self.corpo["referentes"].get(referente)
        if r is None:
            raise ReferencialError(f"referente inexistente: {referente}")
        if _tipo_referente(r) != "rel" or r["elemento"] != elemento:
            raise ReferencialError(f"{referente} não está sobre o elemento {elemento}")
        return r["de"]

    def azimute(self, elemento, de):
        """Azimute verdadeiro em 'de' (ponta), apontando para a outra ponta."""
        a, b = self.pontas(elemento)
        if de not in (a, b):
            raise ReferencialError(f"{de} não é ponta de {elemento}")
        outra = b if de == a else a
        p, q = self.referente(de), self.referente(outra)
        az, _, _ = GEOD.inv(p[1], p[0], q[1], q[0])
        return az % 360

    @staticmethod
    def _andar(lat, lon, az, s, o):
        """Anda s pela geodésica de azimute az, depois o à direita. Devolve (lat, lon, az no ponto do eixo)."""
        if s >= 0:
            lon2, lat2, az_volta = GEOD.fwd(lon, lat, az, s)
            az_p = (az_volta + 180) % 360
        else:
            lon2, lat2, az_volta = GEOD.fwd(lon, lat, (az + 180) % 360, -s)
            az_p = az_volta % 360
        if o:
            lon2, lat2, _ = GEOD.fwd(lon2, lat2, (az_p + 90) % 360 if o > 0 else (az_p - 90) % 360, abs(o))
        return lat2, lon2, az_p

    def seed(self, seed: dict):
        """(lat, lon, azimute do eixo no ponto, no sentido de s).

        Seed sem elemento é a posição do próprio referente (um nó): sem eixo não
        há direção, então s e o precisam ser zero (regra 3).
        """
        elemento, ref = seed.get("elemento"), seed["referente"]
        if elemento is None:
            if float(seed.get("s") or 0) != 0 or float(seed.get("o") or 0) != 0:
                raise ReferencialError("seed sem elemento não pode andar: um ponto sozinho não define direção (regra 3)")
            lat, lon = self.referente(ref)
            return lat, lon, None
        de = self.sentido(elemento, ref)
        if ref == de:
            lat, lon = self.referente(ref)
            az = self.azimute(elemento, de)
        else:
            lat, lon, az = self._ponto_e_azimute(ref)
        return self._andar(lat, lon, az, float(seed["s"]), float(seed["o"]))

    def inverso(self, elemento, referente, lat, lon, tol=1e-8, max_iter=30):
        """(s, o) tais que seed(elemento, referente, s, o) cai em (lat, lon).

        Newton sobre a própria função de resolução, até 1e-8 m: a seed gerada
        reproduz o ponto exatamente, o que mantém a envoltória conservadora
        depois da ida e volta pelo ledger.
        """
        alvo_lon, alvo_lat = lon, lat
        p0 = self.referente(referente) if referente in self.pontas(elemento) else self._ponto_e_azimute(referente)[:2]
        az = self.azimute(elemento, self.sentido(elemento, referente)) if referente in self.pontas(elemento) \
            else self._ponto_e_azimute(referente)[2]
        az12, _, d = GEOD.inv(p0[1], p0[0], alvo_lon, alvo_lat)
        from math import cos, radians, sin
        s = d * cos(radians(az12 - az))
        o = d * sin(radians(az12 - az))

        def erro(s_, o_):
            la, lo, _ = self.seed({"elemento": elemento, "referente": referente, "s": s_, "o": o_})
            azv, _, dv = GEOD.inv(alvo_lon, alvo_lat, lo, la)
            return dv * sin(radians(azv)), dv * cos(radians(azv))      # (leste, norte) do resolvido − alvo
        for _ in range(max_iter):
            ex, ey = erro(s, o)
            if (ex * ex + ey * ey) ** 0.5 < tol:
                return s, o
            h = 1e-3
            ax, ay = erro(s + h, o); bx, by = erro(s, o + h)
            j11, j21 = (ax - ex) / h, (ay - ey) / h
            j12, j22 = (bx - ex) / h, (by - ey) / h
            det = j11 * j22 - j12 * j21
            s -= (j22 * ex - j12 * ey) / det
            o -= (-j21 * ex + j11 * ey) / det
        raise ReferencialError("inversa da seed não convergiu")


# ---------------------------------------------------------------- integração com o ledger

def seeds_do_estado(plano: dict) -> dict:
    """{base_do_path: seed} para todo trecho do corpo com forma de seed."""
    out = {}
    for p in plano:
        if p.endswith(".referente"):
            base = p[: -len(".referente")]
            out[base] = {k: plano.get(f"{base}.{k}") for k in ("elemento", "referente", "s", "o")}
    return out


def referencial_vigente(conn, run_id, t, cur=None):
    """(ref_id, corpo) do airport vivo em t, ou (None, None)."""
    fechar = cur is None
    cur = cur or conn.cursor()
    try:
        cur.execute("SELECT feature_id FROM features_de_tipo(%s, 'airport')", (run_id,))
        aeros = [r[0] for r in cur.fetchall()]
        vivos = []
        for a in aeros:
            cur.execute("SELECT viva_em(%s, %s, %s)", (run_id, a, t))
            if cur.fetchone()[0]:
                vivos.append(a)
        if not vivos:
            return None, None
        if len(vivos) > 1:
            raise ReferencialError("mais de um airport vivo no mesmo run")
        cur.execute("SELECT valor FROM estado_em(%s, %s, %s, 'referencial.') WHERE path = 'referencial.ref_id'",
                    (run_id, vivos[0], t))
        r = cur.fetchone()
        if r is None:
            return None, None
        ref_id = r[0]
        cur.execute("SELECT corpo FROM referencial WHERE ref_id = %s", (ref_id,))
        c = cur.fetchone()
        return ref_id, (c[0] if c else None)
    finally:
        if fechar:
            cur.close()


def _checar_seeds(res: Resolvedor, plano: dict, quem: str):
    for base, s in seeds_do_estado(plano).items():
        if s["s"] is None or s["o"] is None:
            raise ReferencialError(f"{quem}: seed incompleta em {base}")
        try:
            if s["elemento"] is None:
                res.seed(s)                     # nó: só o referente, sem andar
            else:
                res.sentido(s["elemento"], s["referente"])
        except ReferencialError as e:
            raise ReferencialError(f"{quem}: seed em {base} aponta para referente ou elemento inexistente ({e})")


def validar_escrita(conn, run_id, feature_id, feature_type, resultante: dict, t):
    """Chamada por append_event antes de gravar. Levanta LedgerError na violação."""
    from .ledger import LedgerError
    with conn.cursor() as cur:
        try:
            if feature_type == "airport":
                ref_id = resultante.get("referencial.ref_id")
                if ref_id is None:
                    return
                cur.execute("SELECT corpo FROM referencial WHERE ref_id = %s", (ref_id,))
                c = cur.fetchone()
                if c is None:
                    raise ReferencialError("airport aponta para referencial não publicado")
                res = Resolvedor(c[0])
                # trocar o referencial não pode deixar seed órfã
                cur.execute("""SELECT DISTINCT feature_id FROM event WHERE run_id = %s
                               AND feature_type <> 'airport' AND event_id <= %s""", (run_id, t))
                for (fid,) in cur.fetchall():
                    cur.execute("SELECT viva_em(%s, %s, %s)", (run_id, fid, t))
                    if not cur.fetchone()[0]:
                        continue
                    cur.execute("SELECT path, valor FROM estado_em(%s, %s, %s)", (run_id, fid, t))
                    _checar_seeds(res, dict(cur.fetchall()), fid)
                return
            if not seeds_do_estado(resultante):
                return
            _, corpo = referencial_vigente(conn, run_id, t, cur)
            if corpo is None:
                raise ReferencialError("seed sem referencial vigente (falta o airport com ref_id)")
            _checar_seeds(Resolvedor(corpo), resultante, feature_id)
        except ReferencialError as e:
            raise LedgerError(str(e)) from e
