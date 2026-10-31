-- Esquema do ledger do aeroledger.
--
-- Princípios que o esquema materializa:
--   * o ledger só recebe INSERT, e só pela função append_event (aeroledger/ledger.py);
--   * transaction-time é a única linha do tempo: o event_id carrega o instante
--     do relógio de simulação, e nenhum evento entra antes de outro já escrito;
--   * o corpo da feature não tem esquema: cada evento grava linhas (path, op, next);
--   * nada derivado é armazenado: o instante sai do event_id, o estado sai da
--     dobra dos eventos, o comprimento sai do referencial.
--
-- PostGIS não é necessário para este arquivo; ver 03_postgis.sql (opcional).

CREATE TABLE IF NOT EXISTS run (
    run_id          TEXT PRIMARY KEY,
    shard           INTEGER NOT NULL CHECK (shard BETWEEN 0 AND 1023),
    origem          TIMESTAMPTZ NOT NULL,          -- instante inicial declarado uma vez
    relogio         TIMESTAMPTZ NOT NULL,          -- relógio de simulação; só avança
    CHECK (relogio >= origem)
);

-- Corpo do referencial, endereçado pelo conteúdo: ref_id = sha256(JCS(corpo)).
-- Imutável. A publicação de um referencial é um DELTA na feature airport.
CREATE TABLE IF NOT EXISTS referencial (
    ref_id          CHAR(64) PRIMARY KEY,
    corpo           JSONB NOT NULL
);

CREATE TABLE IF NOT EXISTS event (
    event_id        BIGINT PRIMARY KEY,            -- snowflake: 41 bits ms desde 2000-01-01 | 10 shard | 12 sequência
    run_id          TEXT   NOT NULL REFERENCES run(run_id),
    feature_id      TEXT   NOT NULL,
    feature_type    TEXT   NOT NULL,
    event_type      TEXT   NOT NULL CHECK (event_type IN ('BASELINE', 'DELTA', 'VOID')),
    asserted_by     TEXT   NOT NULL CHECK (length(asserted_by) > 0),
    owner_ref       TEXT,
    reason          TEXT,
    caused_by       BIGINT REFERENCES event(event_id),
    prev_hash       CHAR(64),
    grupo_id        BIGINT,                        -- event_id do primeiro evento do grupo (append_group)
    grupo_n         INTEGER,                       -- quantos eventos o grupo tem
    hash            CHAR(64) NOT NULL,
    CHECK (event_type <> 'VOID' OR reason IS NOT NULL),
    CHECK (caused_by IS NULL OR caused_by < event_id),
    CHECK ((grupo_id IS NULL) = (grupo_n IS NULL)),
    CHECK (grupo_id IS NULL OR (grupo_id <= event_id AND grupo_n >= 2)),
    UNIQUE (event_id, run_id, feature_id)          -- alvo da FK de event_field
);

-- Linhas do corpo. run_id e feature_id repetem o envelope só para indexar a
-- leitura por (feature, camada); a FK composta garante que não divergem.
CREATE TABLE IF NOT EXISTS event_field (
    event_id        BIGINT NOT NULL,
    run_id          TEXT   NOT NULL,
    feature_id      TEXT   NOT NULL,
    path            TEXT   NOT NULL COLLATE "C",   -- 'geometria.primitivos.p1.largura'
    op              TEXT   NOT NULL CHECK (op IN ('set', 'removed')),
    next            JSONB,                         -- valor JSON; null JSON ≠ ausente
    PRIMARY KEY (event_id, path),
    FOREIGN KEY (event_id, run_id, feature_id) REFERENCES event (event_id, run_id, feature_id),
    CHECK (op = 'set' OR next IS NULL)
);

CREATE INDEX IF NOT EXISTS event_feature_idx   ON event (run_id, feature_id, event_id);
CREATE INDEX IF NOT EXISTS event_type_idx      ON event (run_id, feature_type, event_id);
CREATE INDEX IF NOT EXISTS event_owner_idx     ON event (owner_ref) WHERE owner_ref IS NOT NULL;
-- ciclo de vida: achar o último BASELINE/VOID sem passar pelos DELTAs
CREATE INDEX IF NOT EXISTS event_ciclo_idx     ON event (run_id, feature_id, event_id) WHERE event_type IN ('BASELINE', 'VOID');
-- features de um tipo por varredura saltada do índice, sem ler todos os eventos
CREATE INDEX IF NOT EXISTS event_tipo_feature_idx ON event (run_id, feature_type, feature_id);
-- reconstrução por feature e por camada (prefixo do path)
CREATE INDEX IF NOT EXISTS field_feature_path_idx ON event_field (run_id, feature_id, path, event_id DESC);

-- Caches. Nunca são fonte de verdade: podem ser apagados a qualquer momento
-- sem perda, e o teste de apêndice "snapshot" prova que dão o mesmo resultado.
CREATE TABLE IF NOT EXISTS snapshot (
    run_id          TEXT   NOT NULL,
    feature_id      TEXT   NOT NULL,
    ate_event_id    BIGINT NOT NULL,               -- estado ao fim deste evento
    ciclo_baseline  BIGINT NOT NULL,               -- BASELINE do ciclo a que pertence
    estado          JSONB  NOT NULL,               -- {path: valor}
    PRIMARY KEY (run_id, feature_id, ate_event_id)
);

CREATE TABLE IF NOT EXISTS projecao_cache (
    run_id          TEXT   NOT NULL,
    chave           CHAR(64) NOT NULL,             -- sha256 da seleção + camadas + instante
    hwm             BIGINT NOT NULL,               -- maior event_id das features selecionadas ≤ t
    conteudo        JSONB  NOT NULL,
    PRIMARY KEY (run_id, chave, hwm)
);
