# aeroledger

Biblioteca Python para guardar dados aeroportuários **geolocalizados e auditáveis**.

Desenvolvida num trabalho de graduação sobre auditoria de dados de infraestrutura aeroportuária. Os experimentos com a pista do SBSJ estão em [aeroledger_notebook](https://github.com/heyfalheiro1537/aeroledger_notebook).

Ela responde a quatro perguntas sobre qualquer dado guardado:

| Pergunta | Como a biblioteca responde |
|---|---|
| Como estava em *t*? | o estado de qualquer instante é reconstruído a partir do histórico, que nunca é apagado |
| Quem afirmou, e por quê? | todo evento tem autor (`asserted_by`), motivo (`reason`) e causa (`caused_by`) |
| O número está certo? | valores derivados (comprimento, rumo, distâncias declaradas) são recalculados das premissas, nunca guardados |
| Alguém mexeu no banco? | os eventos são encadeados por hash; alterar, apagar ou inserir quebra a cadeia |
| Quem lê sem acesso ao banco pode confiar? | a leitura vem de um tópico com o log do run; cada consumidor confere a cadeia, monta só o que precisa e responde com limite explícito |

---

## Instalação

Precisa de Python 3.12+ e de um PostgreSQL 14+ (PostGIS é opcional).

```sh
pip install git+https://github.com/heyfalheiro1537/aeroledger

# ou, para desenvolver:
git clone https://github.com/heyfalheiro1537/aeroledger && cd aeroledger
pip install -e ".[testes]"
```

Para ter um PostgreSQL em `localhost:54329`, use um destes:

```sh
scripts/pg_local.sh          # PostgreSQL instalado na máquina
docker compose up -d         # ou via Docker
```

A conexão usa o argumento `dsn` de `conectar`, ou a variável `AEROLEDGER_DSN`, ou o padrão `postgresql://aeroledger@localhost:54329/aeroledger`.

---

## Em cinco minutos

```python
from datetime import datetime, timezone
import aeroledger as al

conn = al.conectar()
al.criar_esquema(conn)                                    # idempotente

# 1. Uma execução com relógio próprio, que só avança
run = al.novo_run(conn, datetime(2026, 10, 1, tzinfo=timezone.utc))

# 2. O referencial: os poucos pontos medidos em que tudo se apoia
ref = al.publicar_referencial(conn, {
    "datum": "SIRGAS2000",
    "origem_local": "THR16",
    "referentes": {
        "THR16": {"lat": -23.219664, "lon": -45.871967},
        "THR34": {"lat": -23.236725, "lon": -45.853461},
    },
    "elementos_lineares": {"RWY_16_34": ["THR16", "THR34"]},
})
al.escrever(conn, run, "SBSJ", "airport", "BASELINE", asserted_by="DECEA",
            corpo={"referencial": {"ref_id": ref}})

# 3. A pista se apoia nas cabeceiras; o comprimento não é escrito
def seed(ref, s=0, o=0):
    return {"elemento": "RWY_16_34", "referente": ref, "s": s, "o": o}

al.escrever(conn, run, "SBSJ-RWY", "runway", "BASELINE", asserted_by="DECEA",
            corpo={"adc": {"direcoes": {"inicio": "16", "fim": "34"}},
                   "geometria": {"primitivos": {"pista": {
                       "tipo": "retangulo", "inicio": seed("THR16"), "fim": seed("THR34"), "largura": 45}}}})

# 4. Ler e derivar
al.estado(conn, run, "SBSJ-RWY")                          # o corpo como estava agora
al.comprimento(al.carregar_referencial(conn, ref), "RWY_16_34")   # calculado, em metros
al.distancias_declaradas(conn, run)                       # {'16': {'TORA': …}, '34': {…}}

# 5. Auditar
al.verificar_cadeia(conn, run)                            # [] = íntegra
al.cabeca(conn, run)                                      # (event_id, hash): guarde fora do banco
```

---

## Conceitos

**Evento.** A única forma de escrever. Três tipos:

- `BASELINE`: cria a feature (ou recria depois de um `VOID`), com o corpo completo;
- `DELTA`: muda campos (`alteracoes={"caminho.do.campo": valor}`; `al.REMOVIDO` apaga um campo);
- `VOID`: encerra o ciclo de vida da feature; exige `reason`.

Não existe atualização nem exclusão. Corrigir é escrever um evento novo.

**Envelope e corpo.** O envelope (`feature_id`, `feature_type`, `asserted_by`, `owner_ref`, `reason`, `caused_by`) é fixo por evento; `feature_type` e `owner_ref` não mudam ao longo da vida da feature. O corpo é JSON livre, organizado em camadas (`adc`, `geometria`, ...), e cada campo vira uma linha no banco. Campo ausente é diferente de campo `null`.

**Tempo.** Cada `run` tem um relógio de simulação que só avança (`al.avancar(conn, run, dias=30)`). O identificador do evento carrega o instante; nada se escreve no passado. Para ler, `t` pode ser um `datetime` (estado ao fim daquele instante), um `event_id` (estado ao fim daquele evento) ou `None` (agora).

**Referencial e seeds.** As posições não são guardadas em latitude e longitude, e sim relativas a **referentes** (ISO 19148):

```python
{"elemento": "RWY_16_34", "referente": "THR34", "s": -283, "o": 0}
#  ao longo do eixo da pista, 283 m para fora a partir da THR34, sobre o eixo
```

- `s`: distância ao longo do eixo, positiva no sentido do outro referente;
- `o`: afastamento lateral, positivo à direita.

Um referente é georreferenciado (`lat`, `lon`) ou relativo a outro (`elemento`, `de`, `s`, `o`), nunca os dois; os referentes não formam ciclos; e um ponto sozinho não define direção. O referencial é endereçado pelo conteúdo (`ref_id` = SHA-256) e sua troca é um `DELTA` no `airport`. Como nenhuma seed guarda comprimento, remedir uma cabeceira é **um** evento, e tudo o que se apoia nela acompanha.

**Derivados.** Comprimento, azimutes, rumo magnético e distâncias declaradas (TORA, TODA, ASDA, LDA) são calculados na leitura, pela geodésica sobre o GRS80 e pela regra do Anexo 14. Nunca são escritos.

**Grupo.** Quando uma mudança precisa de vários eventos (a carga de uma edição do AIP, por exemplo), `escrever_grupo` os grava juntos: ou todos, ou nenhum, contíguos na cadeia e no mesmo instante. O envelope de cada um leva `grupo_id` (o `event_id` do primeiro) e `grupo_n` (quantos são). Quem lê pelo tópico nunca vê o grupo pela metade.

**Cadeia de hash.** `hash = SHA-256(JCS(envelope + prev_hash + campos))`, com JCS = RFC 8785. `grupo_id` e `grupo_n` só entram no conteúdo quando o evento é de um grupo; assim, um evento fora de grupo tem o mesmo hash que na versão 0.5.0. Qualquer terceiro pode reimplementar a verificação a partir dessa especificação (o repositório dos experimentos tem um verificador independente que não importa esta biblioteca). Alterações que não deixam sucessor (mexer só no último evento, truncar, reescrever tudo) só são detectadas comparando a cabeça com uma âncora guardada fora do banco.

**Escrita e leitura separadas (CQRS).** O ledger no PostgreSQL é a única fonte de verdade e serve à escrita, que precisa ser consistente. A leitura não fala com ele:

```
ledger ──retransmitir──▶ tópico geral ──consumir──▶ consumidor (projeção local)
```

- **Tópico geral.** O log ordenado do run publicado para fora do banco (CDC). O retransmissor não guarda estado (o cursor é o último evento no tópico), não espera ninguém e pode refazer o tópico do zero a partir do ledger. Antes do evento que adota um referencial, publica o corpo dele, endereçado pelo conteúdo. É um tópico só, com tudo; tópicos por nó seriam otimização derivada deste.
- **Canal não confiável.** O consumidor recalcula o hash de cada evento e confere o elo `prev_hash` antes de aplicar. Mensagem alterada, removida, inserida ou fora de ordem para o consumo ali (`TopicoCorrompido`); se o elo não fecha, ele volta ao ponto comum (bifurcação) ou descarta o último aplicado, e fica num prefixo confiável. Entrega repetida e idêntica é ignorada.
- **Consumidor.** Lê tudo, com checkpoint, no ritmo dele (laço, cron, uma vez). Grupos só entram inteiros. Na leitura, dobra só o **fecho de dependência** das raízes que assinou (arestas declaradas: `owner_ref` e valores iguais ao id de outra feature, nos dois sentidos; quem tem seed depende do `airport`) e deriva localmente.
- **Limite explícito.** `ler(ate)` exige um `event_id` ou um instante; não existe "agora". Mesmo limite, mesma resposta em qualquer consumidor. Se o limite passa do que o consumidor recebeu, ele recusa (`ConsumidorAtrasado`): ler o que se escreveu é pedir "pelo menos N".
- **Resposta como dado.** `{run_id, ate, cabeca, raizes, fecho, estados, referencial, derivados, regra, hash_derivados, hash}`, com `regra` = SHA-256 do código que derivou.

O que o encadeamento não prova: que o consumidor recebeu **tudo**. Um tópico cortado no fim parece só atrasado, e um evento falso anexado no fim passa pelo elo; os dois só aparecem comparando a cabeça do consumidor com a âncora (ou com o ledger).

---

## API

Tudo abaixo está em `import aeroledger as al`. Os módulos completos (`al.ledger`, `al.referencial`, ...) também estão disponíveis.

### Banco e tempo

| Função | O que faz |
|---|---|
| `conectar(dsn=None)` | abre uma conexão |
| `criar_esquema(conn, postgis=False)` | cria tabelas, índices e funções; idempotente |
| `novo_run(conn, origem, run_id=None)` | nova execução com relógio próprio; devolve `run_id` |
| `avancar(conn, run, dias=0, horas=0, minutos=0, segundos=0)` | avança o relógio (nunca recua) |
| `agora(conn, run)` | instante atual do relógio |
| `instante(event_id)` | instante em que o evento foi escrito |

### Escrita

| Função | O que faz |
|---|---|
| `escrever(conn, run, feature_id, feature_type, event_type, *, asserted_by, corpo=None, alteracoes=None, reason=None, owner_ref=None, caused_by=None)` | escreve um evento; devolve `(event_id, hash)`; levanta `LedgerError` se a forma for inválida |
| `escrever_grupo(conn, run, [ {feature_id, feature_type, event_type, asserted_by, ...}, ... ])` | escreve dois ou mais eventos como uma mudança só (tudo ou nada); devolve `[(event_id, hash)]` |
| `publicar_referencial(conn, corpo)` | valida e grava um referencial; devolve `ref_id` (não escreve evento) |

`escrever` recusa, sem gravar nada: tipo inválido; `DELTA`/`VOID` sem `BASELINE` vigente; `BASELINE` sobre feature viva; envelope alterado; `VOID` sem motivo; autor vazio; caminho malformado; remoção de campo ausente; campo que é folha e prefixo ao mesmo tempo; `caused_by` inexistente; seed apontando para referente inexistente; troca de referencial que deixaria seed órfã.

### Leitura e auditoria

| Função | O que faz |
|---|---|
| `estado(conn, run, feature_id, t=None, camada=None, plano=False)` | estado em `t`; `None` se a feature não existe em `t`; `camada` lê só uma parte do corpo |
| `features(conn, run, t=None, tipos=None)` | features vivas em `t` |
| `ciclos(conn, run, feature_id)` | `[(event_id, tipo, nº do ciclo)]` |
| `diff(conn, run, t0, t1)` | `[(feature_id, caminho, antes, depois)]`; `al.AUSENTE` marca campo inexistente |
| `envelope(conn, event_id)` | quem, quando, por quê e com qual causa |
| `verificar_cadeia(conn, run)` | `[]` se íntegra; senão, o primeiro `event_id` divergente |
| `cabeca(conn, run)` | `(event_id, hash)` do último evento, para guardar como âncora |

### Referencial e derivações

| Função | O que faz |
|---|---|
| `carregar_referencial(conn, ref_id)` | corpo do referencial |
| `referencial_vigente(conn, run, t_limite)` | `(ref_id, corpo)` vigente (use `al.relogio.limite(t)` para um `datetime`) |
| `Resolvedor(corpo).seed(seed)` | `(lat, lon, azimute do eixo)` de uma seed |
| `comprimento(corpo, elemento)` | comprimento geodésico do elemento linear (m) |
| `azimutes(corpo, elemento)` | azimute verdadeiro em cada ponta |
| `rumo_magnetico(azimute, variacao_leste)` | rumo magnético (oeste negativo) |
| `distancias_declaradas(conn, run, t=None)` | TORA, TODA, ASDA e LDA de cada cabeceira em `t` |

### Lentes (consumidores)

| Função | O que faz |
|---|---|
| `adc.projetar(conn, run, t=None)` | o conteúdo de uma carta de aeródromo em `t`: geometria (gerada), nós e tabela de distâncias; lê só as camadas `adc` e `geometria` |
| `projecao.projetar(conn, run, t=None, tipos=None, camadas=None)` | leitura por tipo e camada, com cache invalidado só por eventos da própria seleção |
| `motor.gerar(geometria, corpo_referencial)` | geometria (shapely, plano local em metros) a partir das seeds; demonstração |

### Tópico e consumidores (`al.topico`)

| Função | O que faz |
|---|---|
| `TopicoMemoria()` | tópico em memória (`anexar`, `ler(desde, maximo)`, `fim()`): o contrato que um adaptador para Kafka cumpriria |
| `retransmitir(conn, run, topico)` | CDC: anexa os eventos do ledger que ainda não estão no tópico; devolve quantos |
| `Consumidor(topico, raizes=None, nome=...)` | projeção local; `raizes=None` assina todas as features |
| `c.consumir(maximo=None)` | lê a partir do checkpoint, confere e aplica; devolve quantos eventos aplicou |
| `c.ler(ate)` | estado e derivados do fecho ao fim de `ate` (`event_id` ou `datetime`), como dado |
| `c.cabeca`, `c.offset`, `c.em_dia()` | último evento aplicado, checkpoint, e se há mensagem nova |
| `c.salvar(caminho)`, `Consumidor.carregar(caminho, topico)` | checkpoint em arquivo; ao retomar, o log local é conferido de novo |

### Erros

`LedgerError` (forma do evento), `ReferencialError` (regras do referencial), `RelogioError` (tempo), `RegraPendente` (derivação sem regra registrada, como barreira de contenção), `topico.TopicoCorrompido` (mensagem que não confere; tem `offset` e `motivo`), `topico.ConsumidorAtrasado` (limite além do que o consumidor recebeu).

---

## Testes

```sh
scripts/pg_local.sh          # ou: docker compose up -d
python3 -m pytest -q
```

Os 57 testes usam um aeródromo fictício e criam um banco descartável por teste. Para outro servidor, defina `AEROLEDGER_DSN_ADMIN` (conexão ao banco `postgres`, com permissão para criar bancos). Cobrem: campo ausente ≠ null, ciclo de vida, snapshot igual à reconstrução, as rejeições de escrita, as regras do referencial, a resolução de seeds, a remedição sem escrita em seed, as derivações, o cache de projeção, os grupos (contíguos, no hash, tudo ou nada) e o tópico: consumidor igual ao ledger em cada fronteira, corte que nunca cai dentro de um grupo, cadências diferentes e retomada após queda chegando ao mesmo hash, subconjunto com os mesmos derivados, canal adulterado (remover, alterar, alterar e recalcular, inserir), entrega repetida, referencial adulterado ou ausente e leitura com o ledger fora.

## Limites conhecidos

- `asserted_by` é texto: registra quem afirmou, mas não prova (sem assinatura digital).
- Sem âncora externa, quem controla o banco inteiro pode reescrever a cadeia sem ser detectado pela própria biblioteca.
- A regra de distâncias declaradas com barreira de contenção não está implementada (`RegraPendente`).
- O motor de geometria (retângulo, ponto, arco) é demonstração; não foi validado contra cartas.
- O tópico é em memória: o transporte real (Kafka), tópicos por nó, confidencialidade (quem assina recebe tudo) e ordem entre runs (aeroportos) ficam fora; o consumidor guarda o log inteiro e filtra só na leitura.
- A escrita ainda serializa por run com lock pessimista (`SELECT … FOR UPDATE`); o lock otimista com versão por feature está decidido e não implementado.
