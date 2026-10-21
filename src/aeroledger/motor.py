"""Motor generativo: seeds + referencial → geometria aproximada e conservadora.

Agnóstico a consumidor: recebe a camada de geometria de uma feature e o corpo do
referencial, devolve geometria shapely no plano local ENU (metros, origem no
referente 'origem_local'). Não lê nem escreve o banco.

Primitivos (camada geometria.primitivos.<nome>):
  ponto     {tipo, seed}                                            → nó (sem envoltória)
  retangulo {tipo, inicio: seed, fim: seed, largura}                → eixo inicio→fim, ±largura/2
  coroa     {tipo, centro: seed, raio_int, raio_ext, ang_ini, ang_fim}
            ângulos em graus, horários, relativos ao azimute do eixo no centro

A feature é a união dos seus primitivos de área. A coroa é discretizada em N
segmentos (o LOD geométrico, parâmetro da leitura, não do corpo) de forma
conservadora: arco externo circunscrito (vértices em R/cos(Δ/2)) e arco interno
inscrito (vértices em r). Assim a coroa real fica sempre dentro do polígono.
"""
from math import atan2, cos, degrees, radians, sin, sqrt

from shapely.geometry import MultiPoint, Point, Polygon
from shapely.ops import unary_union

from .referencial import Resolvedor

A = 6378137.0
F = 1 / 298.257222101
E2 = F * (2 - F)


# ---------------------------------------------------------------- plano local

def _ecef(lat, lon, h):
    la, lo = radians(lat), radians(lon)
    n = A / sqrt(1 - E2 * sin(la) ** 2)
    return ((n + h) * cos(la) * cos(lo), (n + h) * cos(la) * sin(lo), (n * (1 - E2) + h) * sin(la))


class PlanoLocal:
    """Plano tangente (ENU) num ponto. Em 3 km o erro do plano fica abaixo de 1 mm."""

    def __init__(self, lat0, lon0, h0=0.0):
        self.lat0, self.lon0, self.h0 = lat0, lon0, h0
        self.o = _ecef(lat0, lon0, h0)
        la, lo = radians(lat0), radians(lon0)
        self.r = ((-sin(lo), cos(lo), 0.0),
                  (-sin(la) * cos(lo), -sin(la) * sin(lo), cos(la)))

    def enu(self, lat, lon):
        x, y, z = _ecef(lat, lon, self.h0)
        d = (x - self.o[0], y - self.o[1], z - self.o[2])
        return (sum(a * b for a, b in zip(self.r[0], d)), sum(a * b for a, b in zip(self.r[1], d)))

    def geo(self, e, n, tol=1e-8):
        """Inversa de enu: (leste, norte) → (lat, lon), por Newton até 1e-8 m.

        1e-8 m é o limite numérico das coordenadas ECEF (~6e6 m) em float64.
        """
        from pyproj import Geod
        g = Geod(ellps="GRS80")
        dist = (e * e + n * n) ** 0.5
        az = (90 - __import__("math").degrees(atan2(n, e))) % 360
        lon, lat, _ = g.fwd(self.lon0, self.lat0, az, dist)
        for _ in range(30):
            ex, ny = self.enu(lat, lon)
            de, dn = e - ex, n - ny
            if (de * de + dn * dn) ** 0.5 < tol:
                return lat, lon
            h = 1e-7
            a1 = self.enu(lat + h, lon); a2 = self.enu(lat, lon + h)
            j11, j21 = (a1[0] - ex) / h, (a1[1] - ny) / h
            j12, j22 = (a2[0] - ex) / h, (a2[1] - ny) / h
            det = j11 * j22 - j12 * j21
            lat += (j22 * de - j12 * dn) / det
            lon += (-j21 * de + j11 * dn) / det
        raise ValueError("inversa do plano local não convergiu")

    def az_enu(self, az):
        """Vetor unitário (leste, norte) de um azimute verdadeiro."""
        return sin(radians(az)), cos(radians(az))


def plano_do_referencial(corpo: dict) -> PlanoLocal:
    """Plano tangente na origem_local, sobre o elipsoide (h = 0).

    Altura zero mantém o motor coerente com as derivações (geodésica sobre o
    elipsoide). Um plano na altura do aeródromo
    (~600 m) esticaria as distâncias em 1 + h/R, cerca de 1e-4.
    """
    res = Resolvedor(corpo)
    lat, lon = res.referente(corpo["origem_local"])
    return PlanoLocal(lat, lon, 0.0)


# ---------------------------------------------------------------- primitivos

def _seed_enu(res, plano, seed):
    lat, lon, az = res.seed(seed)
    return plano.enu(lat, lon), az


def retangulo(res, plano, p):
    (a, _), (b, _) = _seed_enu(res, plano, p["inicio"]), _seed_enu(res, plano, p["fim"])
    dx, dy = b[0] - a[0], b[1] - a[1]
    L = sqrt(dx * dx + dy * dy)
    if L == 0:
        raise ValueError("retângulo com início e fim coincidentes")
    nx, ny = dy / L, -dx / L                    # normal à direita do sentido início→fim
    w = float(p["largura"]) / 2
    return Polygon([(a[0] + nx * w, a[1] + ny * w), (b[0] + nx * w, b[1] + ny * w),
                    (b[0] - nx * w, b[1] - ny * w), (a[0] - nx * w, a[1] - ny * w)])


def coroa(res, plano, p, n_segmentos):
    (c, az_eixo) = _seed_enu(res, plano, p["centro"])
    r, R = float(p["raio_int"]), float(p["raio_ext"])
    a0, a1 = float(p["ang_ini"]), float(p["ang_fim"])
    if not (0 <= r < R) or a1 <= a0:
        raise ValueError("coroa inválida: precisa 0 ≤ raio_int < raio_ext e ang_ini < ang_fim")
    n = int(n_segmentos)
    d = (a1 - a0) / n
    Rc = R / cos(radians(d) / 2)                # circunscrito: arestas tangentes ao arco externo

    def ponto(raio, ang):
        az = radians(az_eixo + ang)
        return (c[0] + raio * sin(az), c[1] + raio * cos(az))

    externo = [ponto(Rc, a0 + k * d) for k in range(n + 1)]
    interno = [ponto(r, a0 + k * d) for k in range(n, -1, -1)] if r > 0 else [c]
    return Polygon(externo + interno)


def flecha_minima(raio, ang_total, tolerancia):
    """Menor N com flecha R·(1 − cos(θ/2N)) ≤ tolerância (nível de detalhe da coroa)."""
    n = 1
    while raio * (1 - cos(radians(ang_total / n) / 2)) > tolerancia:
        n += 1
    return n


def gerar(geometria: dict, corpo_referencial: dict, n_segmentos=16):
    """Geometria da feature (união dos primitivos), em ENU.

    geometria: a camada 'geometria' do estado aninhado ({'primitivos': {...}}).
    Devolve (forma, nos): forma é a união dos primitivos de área (ou None),
    nos é a lista de pontos dos primitivos 'ponto'.
    """
    res = Resolvedor(corpo_referencial)
    plano = plano_do_referencial(corpo_referencial)
    areas, nos = [], []
    for nome in sorted(geometria.get("primitivos", {})):
        p = geometria["primitivos"][nome]
        tipo = p.get("tipo")
        if tipo == "ponto":
            nos.append(Point(_seed_enu(res, plano, p["seed"])[0]))
        elif tipo == "retangulo":
            areas.append(retangulo(res, plano, p))
        elif tipo == "coroa":
            areas.append(coroa(res, plano, p, n_segmentos))
        else:
            raise ValueError(f"primitivo desconhecido: {tipo!r} (vocabulário: ponto, retangulo, coroa)")
    forma = unary_union(areas) if areas else None
    return forma, nos


def contar_primitivos(geometria: dict) -> int:
    return len(geometria.get("primitivos", {}))
