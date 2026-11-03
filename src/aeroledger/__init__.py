"""aeroledger: dados aeroportuários geolocalizados e auditáveis.

Um ledger em que só se escreve, encadeado por hash, guarda as afirmações sobre
a infraestrutura (quem afirmou o quê, quando e por quê). As posições se apoiam
em poucos referentes medidos (referenciamento linear, ISO 19148), e tudo o que
é derivável (comprimento, rumo, distâncias declaradas, geometria) é calculado
na leitura, nunca armazenado.

Uso típico:

    import aeroledger as al

    conn = al.conectar()
    al.criar_esquema(conn)
    run = al.novo_run(conn, origem)
    ref = al.publicar_referencial(conn, corpo)
    al.escrever(conn, run, "SBSJ", "airport", "BASELINE", asserted_by="DECEA",
                corpo={"referencial": {"ref_id": ref}})
    al.estado(conn, run, "SBSJ")
    al.verificar_cadeia(conn, run)          # [] = íntegra

Leitura separada da escrita (CQRS): o ledger é publicado num tópico geral e
cada consumidor monta, do lado dele, só o que precisa:

    top = al.topico.TopicoMemoria()
    al.topico.retransmitir(conn, run, top)          # CDC: ledger → tópico
    c = al.topico.Consumidor(top, raizes=["SBSJ-RWY"])
    c.consumir()
    c.ler(ate=event_id)                             # limite explícito, nunca "agora"

Módulos (para quem precisa de mais que a fachada):
    ledger       escrita e leitura do ledger
    referencial  referentes, seeds e o Resolvedor
    derivacoes   comprimento, azimute, rumo, distâncias declaradas
    motor        geometria a partir das seeds (demonstração)
    adc          lente ADC: o que uma carta de aeródromo lê do ledger
    projecao     leitura por seleção e camada, com cache
    topico       tópico geral (CDC do ledger) e consumidores com checkpoint
    relogio      relógio de simulação e identificadores
    canonico     serialização canônica (JCS) e SHA-256
    banco        conexão e esquema
"""
from . import adc, banco, canonico, derivacoes, ledger, motor, projecao, referencial, relogio, topico
from .banco import conectar, criar_esquema
from .derivacoes import RegraPendente, azimutes, comprimento, distancias_declaradas, rumo_magnetico
from .ledger import AUSENTE, REMOVIDO, LedgerError
from .ledger import append_event as escrever
from .ledger import append_group as escrever_grupo
from .ledger import cabeca, ciclos, diff, envelope, features
from .ledger import reconstruct_state as estado
from .ledger import verify_chain as verificar_cadeia
from .referencial import ReferencialError, Resolvedor
from .referencial import carregar as carregar_referencial
from .referencial import publicar as publicar_referencial
from .referencial import referencial_vigente
from .relogio import RelogioError, agora, avancar, instante, novo_run

__version__ = "0.7.0"

__all__ = [
    # banco
    "conectar", "criar_esquema",
    # relógio
    "novo_run", "avancar", "agora", "instante",
    # escrita e leitura
    "escrever", "escrever_grupo", "estado", "features", "ciclos", "diff", "envelope", "cabeca", "verificar_cadeia",
    "REMOVIDO", "AUSENTE",
    # referencial
    "publicar_referencial", "carregar_referencial", "referencial_vigente", "Resolvedor",
    # derivações
    "comprimento", "azimutes", "rumo_magnetico", "distancias_declaradas",
    # erros
    "LedgerError", "ReferencialError", "RelogioError", "RegraPendente",
    # módulos
    "adc", "banco", "canonico", "derivacoes", "ledger", "motor", "projecao", "referencial", "relogio", "topico",
]
