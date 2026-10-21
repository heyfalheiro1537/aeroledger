-- Consultas de leitura do ledger como funções SQL.
-- As cinco consultas da arquitetura: estado em t, ciclos de vida, diff entre
-- instantes (montado em Python sobre estado_em), staleness da projeção e
-- ligação da cadeia (a verificação completa recalcula o hash em Python).

-- Instante (ms desde 2000-01-01 UTC) de um event_id.
CREATE OR REPLACE FUNCTION instante_de(eid BIGINT) RETURNS TIMESTAMPTZ
LANGUAGE sql IMMUTABLE AS $$
    SELECT TIMESTAMPTZ '2000-01-01 00:00:00+00' + ((eid >> 22) * INTERVAL '1 millisecond')
$$;

-- Maior event_id possível ao fim de um instante: resolve o "t ambíguo".
CREATE OR REPLACE FUNCTION limite_de(t TIMESTAMPTZ) RETURNS BIGINT
LANGUAGE sql IMMUTABLE AS $$
    SELECT ((floor(extract(epoch FROM (t - TIMESTAMPTZ '2000-01-01 00:00:00+00')) * 1000)::BIGINT + 1) << 22) - 1
$$;

-- Q1 — estado de uma feature ao fim do evento t, no ciclo de vida vigente.
-- Devolve zero linhas se a feature não existe em t (nunca criada, ou VOID).
-- prefixo: opcional; restringe a uma camada ('geometria.') sem ler as outras.
CREATE OR REPLACE FUNCTION estado_em(p_run TEXT, p_feature TEXT, p_t BIGINT, p_prefixo TEXT DEFAULT NULL)
RETURNS TABLE (path TEXT, valor JSONB)
LANGUAGE sql STABLE AS $$
    WITH ciclo AS (
        SELECT max(event_id) AS b
        FROM event
        WHERE run_id = p_run AND feature_id = p_feature
          AND event_type = 'BASELINE' AND event_id <= p_t
    ), vivo AS (
        SELECT c.b FROM ciclo c
        WHERE c.b IS NOT NULL
          AND NOT EXISTS (SELECT 1 FROM event e
                          WHERE e.run_id = p_run AND e.feature_id = p_feature
                            AND e.event_type = 'VOID' AND e.event_id > c.b AND e.event_id <= p_t)
    ), ultimo AS (
        SELECT DISTINCT ON (f.path) f.path, f.op, f.next
        FROM event_field f, vivo v
        WHERE f.run_id = p_run AND f.feature_id = p_feature
          AND f.event_id BETWEEN v.b AND p_t
          AND (p_prefixo IS NULL OR (f.path >= (p_prefixo COLLATE "C") AND f.path < ((p_prefixo || chr(1114111)) COLLATE "C")))
        ORDER BY f.path, f.event_id DESC
    )
    SELECT path, next FROM ultimo WHERE op = 'set'
$$;

-- A feature existe em t?
CREATE OR REPLACE FUNCTION viva_em(p_run TEXT, p_feature TEXT, p_t BIGINT) RETURNS BOOLEAN
LANGUAGE sql STABLE AS $$
    SELECT coalesce((
        SELECT event_type <> 'VOID' FROM event
        WHERE run_id = p_run AND feature_id = p_feature AND event_id <= p_t
          AND event_type IN ('BASELINE', 'VOID')
        ORDER BY event_id DESC LIMIT 1), false)
$$;

-- Q2 — segmentação dos ciclos de vida de uma feature.
CREATE OR REPLACE FUNCTION ciclos(p_run TEXT, p_feature TEXT)
RETURNS TABLE (event_id BIGINT, event_type TEXT, ciclo BIGINT)
LANGUAGE sql STABLE AS $$
    SELECT e.event_id, e.event_type,
           sum((e.event_type = 'BASELINE')::int) OVER (ORDER BY e.event_id) AS ciclo
    FROM event e
    WHERE e.run_id = p_run AND e.feature_id = p_feature
    ORDER BY e.event_id
$$;

-- Q4 — staleness: maior event_id de um conjunto de features até t.
CREATE OR REPLACE FUNCTION marca_dagua(p_run TEXT, p_features TEXT[], p_t BIGINT) RETURNS BIGINT
LANGUAGE sql STABLE AS $$
    SELECT max(event_id) FROM event
    WHERE run_id = p_run AND feature_id = ANY (p_features) AND event_id <= p_t
$$;

-- Q5 (só a ligação) — eventos cujo prev_hash não é o hash do anterior.
-- A verificação completa (aeroledger.ledger.verify_chain) também recalcula o hash do
-- conteúdo: esta consulta sozinha não pega alteração de conteúdo sem
-- recálculo do hash, achado já registrado na fase de agosto.
CREATE OR REPLACE FUNCTION ligacao_quebrada(p_run TEXT) RETURNS TABLE (event_id BIGINT)
LANGUAGE sql STABLE AS $$
    SELECT t.event_id FROM (
        SELECT e.event_id, e.prev_hash, lag(e.hash) OVER (ORDER BY e.event_id) AS anterior
        FROM event e WHERE e.run_id = p_run) t
    WHERE t.prev_hash IS DISTINCT FROM t.anterior
    ORDER BY t.event_id
$$;

-- Features de um tipo (varredura saltada: custo proporcional ao nº de features, não de eventos).
CREATE OR REPLACE FUNCTION features_de_tipo(p_run TEXT, p_tipo TEXT) RETURNS TABLE (feature_id TEXT)
LANGUAGE sql STABLE AS $$
    WITH RECURSIVE f(fid) AS (
        (SELECT e.feature_id FROM event e WHERE e.run_id = p_run AND e.feature_type = p_tipo
         ORDER BY e.feature_id LIMIT 1)
        UNION ALL
        SELECT (SELECT e.feature_id FROM event e WHERE e.run_id = p_run AND e.feature_type = p_tipo
                AND e.feature_id > f.fid ORDER BY e.feature_id LIMIT 1)
        FROM f WHERE f.fid IS NOT NULL
    )
    SELECT fid FROM f WHERE fid IS NOT NULL
$$;

-- Tipos presentes num run (mesma varredura saltada).
CREATE OR REPLACE FUNCTION tipos_do_run(p_run TEXT) RETURNS TABLE (feature_type TEXT)
LANGUAGE sql STABLE AS $$
    WITH RECURSIVE t(tipo) AS (
        (SELECT e.feature_type FROM event e WHERE e.run_id = p_run ORDER BY e.feature_type LIMIT 1)
        UNION ALL
        SELECT (SELECT e.feature_type FROM event e WHERE e.run_id = p_run AND e.feature_type > t.tipo
                ORDER BY e.feature_type LIMIT 1)
        FROM t WHERE t.tipo IS NOT NULL
    )
    SELECT tipo FROM t WHERE tipo IS NOT NULL
$$;
