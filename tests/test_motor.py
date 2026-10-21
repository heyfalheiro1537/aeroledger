"""Motor: primitivos, coroa conservadora, LOD, forma B ao trocar o referencial."""
from math import cos, radians, sin

import pytest
from shapely.geometry import Point

from aeroledger import ledger, motor, referencial, relogio
from aeroledger.referencial import Resolvedor
from cenario_sintetico import B, GEOD, corpo_ref, montar, seed


def test_retangulo_da_pista():
    c = corpo_ref()
    forma, nos = motor.gerar({"primitivos": {"pista": {"tipo": "retangulo", "inicio": seed("THR_A"),
                                                       "fim": seed("THR_B"), "largura": 45}}}, c)
    assert nos == []
    # área ≈ comprimento × largura (2000 × 45), com erro do plano tangente desprezível
    assert abs(forma.area - 2000 * 45) < 1.0


@pytest.mark.parametrize("n", [8, 16, 32])
def test_coroa_conservadora(n):
    """A coroa real (amostrada densamente) fica sempre dentro do polígono."""
    c = corpo_ref()
    p = {"tipo": "coroa", "centro": seed("THR_A", s=1000), "raio_int": 150, "raio_ext": 180,
         "ang_ini": 10, "ang_fim": 100}
    forma, _ = motor.gerar({"primitivos": {"c": p}}, c, n_segmentos=n)
    res, plano = Resolvedor(c), motor.plano_do_referencial(c)
    (cx, cy), az = plano.enu(*res.seed(p["centro"])[:2]), res.seed(p["centro"])[2]
    for k in range(2001):
        ang = radians(az + 10 + 90 * k / 2000)
        for r in (150, 165, 180):
            pt = Point(cx + r * sin(ang), cy + r * cos(ang))
            assert forma.buffer(1e-6).contains(pt)


def test_lod_reduz_o_desvio():
    c = corpo_ref()
    p = {"tipo": "coroa", "centro": seed("THR_A", s=1000), "raio_int": 150, "raio_ext": 180,
         "ang_ini": 0, "ang_fim": 90}
    areas = [motor.gerar({"primitivos": {"c": p}}, c, n_segmentos=n)[0].area for n in (8, 16, 32)]
    real = 3.14159265358979 * (180 ** 2 - 150 ** 2) / 4
    excessos = [a - real for a in areas]
    assert all(e > 0 for e in excessos) and excessos[0] > excessos[1] > excessos[2]


def test_flecha_minima():
    n = motor.flecha_minima(180, 90, 1.77)
    assert 180 * (1 - cos(radians(90 / n) / 2)) <= 1.77
    assert 180 * (1 - cos(radians(90 / (n - 1)) / 2)) > 1.77


def test_forma_b_pista_acompanha_a_remedicao(conn):
    """Remedir a ponta B: só o referencial muda, nenhuma seed é escrita, e a pista termina na ponta nova."""
    run = montar(conn)
    relogio.avancar(conn, run, dias=10)
    lon2, lat2, _ = GEOD.fwd(B[1], B[0], 45.0, 1.0)            # ponta B remedida 1 m adiante
    novo = referencial.publicar(conn, corpo_ref(b=(lat2, lon2)))
    with conn.cursor() as cur:
        cur.execute("SELECT max(event_id) FROM event")
        antes = cur.fetchone()[0]
    eid, _ = ledger.append_event(conn, run, "XXXX", "airport", "DELTA", asserted_by="t",
                                 alteracoes={"referencial.ref_id": novo}, reason="remedição da ponta B")
    with conn.cursor() as cur:
        cur.execute("SELECT feature_id, path FROM event_field WHERE event_id > %s", (antes,))
        assert cur.fetchall() == [("XXXX", "referencial.ref_id")]
    e = ledger.reconstruct_state(conn, run, "XXXX-RWY")
    forma, _ = motor.gerar(e["geometria"], referencial.carregar(conn, novo))
    assert abs(forma.area - 2001 * 45) < 1.0
