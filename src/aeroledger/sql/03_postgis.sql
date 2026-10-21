-- Opcional: só no serviço Docker (imagem postgis/postgis).
-- Guarda a geometria das projeções em PostGIS para consulta espacial (GiST).
-- O núcleo do artefato não depende deste arquivo: a geometria é calculada em
-- Python a partir das seeds e do referencial e nunca é fonte de verdade.

CREATE EXTENSION IF NOT EXISTS postgis;

CREATE TABLE IF NOT EXISTS projecao_geom (
    run_id      TEXT   NOT NULL,
    chave       CHAR(64) NOT NULL,
    hwm         BIGINT NOT NULL,
    feature_id  TEXT   NOT NULL,
    geom        geometry(Geometry, 4674) NOT NULL,   -- SIRGAS2000 geográfico
    PRIMARY KEY (run_id, chave, hwm, feature_id)
);

CREATE INDEX IF NOT EXISTS projecao_geom_gist ON projecao_geom USING gist (geom);
