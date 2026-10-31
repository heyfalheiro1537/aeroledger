"""Camada de leitura: o tópico geral e os consumidores (CQRS).

Escrita e leitura ficam separadas. O ledger no PostgreSQL continua a única
fonte de verdade; quem lê não fala com ele.

    ledger ──retransmitir──▶ tópico geral ──consumir──▶ consumidor (projeção local)

Tópico geral. É o log ordenado do run publicado para fora do banco (CDC): o
retransmissor lê os eventos em ordem de event_id e os anexa ao tópico. Ele não
guarda estado (o cursor é o último evento que já está no tópico), não espera
nenhum consumidor (fire-and-forget) e o tópico pode ser refeito do zero a
partir do ledger. Antes do evento que adota um referencial, o retransmissor
publica o corpo desse referencial, endereçado pelo conteúdo; assim o tópico
basta ao consumidor. Há um tópico só, com tudo: o consumidor filtra do lado
dele. Tópicos por nó seriam otimização derivada deste log, não outra fonte.

O canal não é confiável. Um transporte como o Kafka cobre perda e reordenação
em trânsito, mas não erro de quem publica nem adulteração no broker. Por isso
o consumidor confere cada mensagem antes de aplicá-la: recalcula o hash do
evento e exige que prev_hash seja o hash do anterior. A primeira mensagem que
não confere interrompe o consumo ali (TopicoCorrompido); nada depois dela é
aplicado. Se o elo não fecha, o evento recebido ou o último aplicado é falso,
e não há como saber qual: o consumidor volta ao ponto comum (bifurcação) ou
descarta o último aplicado, e fica num prefixo confiável. Mensagem repetida
(entrega pelo menos uma vez) é ignorada se for idêntica à já aplicada.

Consumidor. Lê tudo, com checkpoint (offset e cabeça), no ritmo dele: em
laço, por cron ou uma vez só. Os eventos de um grupo (ledger.append_group) só
são aplicados juntos, então o checkpoint nunca fica no meio de um grupo e
nenhuma leitura mostra uma mudança pela metade. Ele guarda o log localmente
(escreve-se pouco) e, na leitura, dobra só o fecho de dependência das raízes
que assinou e deriva localmente; a informação derivada é dele.

Leitura com limite explícito. ler(ate) exige um event_id ou um instante; não
existe "agora". Com o mesmo limite, a resposta é a mesma em qualquer
consumidor e pode ficar em cache para sempre. Se o limite passa do que o
consumidor já recebeu, ele recusa (ConsumidorAtrasado) em vez de responder
com um estado velho: ler o que se escreveu é pedir "pelo menos N".

A resposta é dado, não objeto: um dict com o limite efetivo, a cabeça da
cadeia nesse limite, o fecho lido, os estados, os derivados, a regra de
derivação (hash do código que derivou) e o hash de tudo isso.

Limites. O encadeamento prova que o consumidor não recebeu nada alterado,
fora de ordem ou com buraco, mas não que recebeu tudo: um tópico cortado no
fim parece só atrasado, e isso só se vê comparando a cabeça com a âncora (ou
com o ledger). Um run é uma cadeia (um aeroporto); ordem entre cadeias,
confidencialidade (quem assina recebe tudo) e o transporte real (Kafka) ficam
fora desta versão.
"""
import bisect
import hashlib
import inspect
import json
from datetime import datetime
from pathlib import Path

from . import derivacoes, relogio
from .canonico import sha256
from .ledger import ENVELOPE, aninhar, conteudo_para_hash
from .referencial import carregar as carregar_referencial

TIPOS_DISTANCIAS = ("runway", "runway_direction", "protect_area", "arresting_gear")
CAMPO_REFERENCIAL = "referencial.ref_id"


class TopicoCorrompido(Exception):
    """Mensagem que não confere: o consumo para nela."""

    def __init__(self, offset, motivo):
        super().__init__(f"offset {offset}: {motivo}")
        self.offset, self.motivo = offset, motivo


class ConsumidorAtrasado(Exception):
    """O limite pedido passa do que o consumidor já recebeu."""


# ---------------------------------------------------------------- tópico

class TopicoMemoria:
    """Tópico em memória: lista em que só se anexa, lida a partir de um offset.

    É o contrato que um adaptador para Kafka (ou para um arquivo) cumpriria:
    anexar(msg) → offset, ler(desde, maximo) → [(offset, msg)], fim().
    As mensagens são copiadas na entrada e na saída, como num broker.
    """

    def __init__(self):
        self.mensagens = []

    def anexar(self, msg):
        self.mensagens.append(json.loads(json.dumps(msg)))
        return len(self.mensagens) - 1

    def ler(self, desde, maximo=None):
        fim = len(self.mensagens) if maximo is None else min(len(self.mensagens), desde + maximo)
        return [(i, json.loads(json.dumps(self.mensagens[i]))) for i in range(desde, fim)]

    def fim(self):
        return len(self.mensagens)


def eventos_do_ledger(conn, run_id, depois_de=None):
    """Eventos do run em ordem, como mensagem: envelope, hash e campos (ordenados por path)."""
    with conn.cursor() as cur:
        cur.execute(f"""SELECT {', '.join(ENVELOPE)}, hash FROM event
                        WHERE run_id = %s AND event_id > %s ORDER BY event_id""",
                    (run_id, depois_de if depois_de is not None else -1))
        eventos = [dict(zip(ENVELOPE + ("hash",), r)) for r in cur.fetchall()]
        if not eventos:
            return []
        cur.execute("""SELECT event_id, path, op, next FROM event_field
                       WHERE run_id = %s AND event_id >= %s ORDER BY event_id, path""",
                    (run_id, eventos[0]["event_id"]))
        campos = {}
        for eid, path, op, nxt in cur.fetchall():
            campos.setdefault(eid, []).append({"path": path, "op": op, "next": nxt})
    for e in eventos:
        e["campos"] = campos.get(e["event_id"], [])
    return eventos


def _cursor(topico):
    """(último event_id no tópico, ref_ids já publicados)."""
    ultimo, refs = None, set()
    for _, msg in topico.ler(0):
        if msg["tipo"] == "evento":
            ultimo = msg["evento"]["event_id"]
        else:
            refs.add(msg["ref_id"])
    return ultimo, refs


def retransmitir(conn, run_id, topico):
    """CDC: anexa ao tópico os eventos do ledger que ainda não estão lá. Devolve quantos.

    Sem estado próprio: o cursor sai do próprio tópico, então rodar de novo não
    duplica nada, e um tópico vazio é refeito do zero.
    """
    ultimo, refs = _cursor(topico)
    n = 0
    for ev in eventos_do_ledger(conn, run_id, ultimo):
        for c in ev["campos"]:
            if c["path"] == CAMPO_REFERENCIAL and c["op"] == "set" and c["next"] not in refs:
                topico.anexar({"tipo": "referencial", "ref_id": c["next"],
                               "corpo": carregar_referencial(conn, c["next"])})
                refs.add(c["next"])
        topico.anexar({"tipo": "evento", "evento": ev})
        n += 1
    return n


# ---------------------------------------------------------------- consumidor

def hash_do_evento(ev):
    return sha256(conteudo_para_hash({k: ev.get(k) for k in ENVELOPE}, ev["campos"]))


def _regra():
    fonte = inspect.getsource(derivacoes).encode("utf-8")
    return {"modulo": "aeroledger.derivacoes", "fonte_sha256": hashlib.sha256(fonte).hexdigest()}


class Consumidor:
    """Projeção local alimentada pelo tópico geral.

    raizes: feature_ids que o consumidor assina; ele lê o fecho de dependência
    delas. None = todas as features do run.
    """

    def __init__(self, topico, raizes=None, nome="consumidor"):
        self.topico = topico
        self.raizes = sorted(raizes) if raizes else None
        self.nome = nome
        self.offset = 0                  # checkpoint: próximo offset a ler
        self.mensagens = []              # eventos aplicados, em ordem: o log local
        self.refs = {}                   # ref_id → corpo, conferido pelo endereço
        self._hash_de = {}               # event_id → hash, dos aplicados
        self._por_feature = {}           # feature_id → [evento]
        self._fronteiras = []            # event_ids onde o log pode ser cortado
        self._offsets = []               # offset seguinte a cada fronteira

    # ------------------------------------------------ estado do consumidor

    @property
    def cabeca(self):
        """(event_id, hash) do último evento aplicado, ou None."""
        if not self.mensagens:
            return None
        e = self.mensagens[-1]
        return e["event_id"], e["hash"]

    def _aplicar(self, eventos, offset):
        for ev in eventos:
            self.mensagens.append(ev)
            self._hash_de[ev["event_id"]] = ev["hash"]
            self._por_feature.setdefault(ev["feature_id"], []).append(ev)
        self._fronteiras.append(eventos[-1]["event_id"])
        self._offsets.append(offset)
        self.offset = offset

    def _recuar(self, ate_event_id):
        """Descarta o que foi aplicado depois da última fronteira ≤ ate_event_id."""
        i = bisect.bisect_right(self._fronteiras, ate_event_id) if ate_event_id is not None else 0
        corte = self._fronteiras[i - 1] if i else None
        manter = [e for e in self.mensagens if corte is not None and e["event_id"] <= corte]
        self.mensagens, self._hash_de, self._por_feature = [], {}, {}
        for e in manter:
            self.mensagens.append(e)
            self._hash_de[e["event_id"]] = e["hash"]
            self._por_feature.setdefault(e["feature_id"], []).append(e)
        self._fronteiras, self._offsets = self._fronteiras[:i], self._offsets[:i]
        self.offset = self._offsets[-1] if i else 0

    def _elo_quebrado(self, off, ev):
        """O elo não fecha: o evento recebido ou o último aplicado é falso, e não há como saber qual.

        Se prev_hash aponta para um evento já aplicado, houve bifurcação: volta-se
        ao ponto comum. Se não aponta para nada conhecido, o último aplicado fica
        sob suspeita e é descartado. Nos dois casos o consumidor fica num prefixo
        que não depende da mensagem duvidosa, e o consumo para.
        """
        comum = next((e["event_id"] for e in self.mensagens if e["hash"] == ev["prev_hash"]), None)
        if comum is not None:
            self._recuar(comum)
            return TopicoCorrompido(off, f"bifurcação depois do evento {comum}: voltou ao ponto comum")
        anterior = self._fronteiras[-2] if len(self._fronteiras) > 1 else None
        self._recuar(anterior)
        return TopicoCorrompido(off, "elo quebrado: prev_hash não é o hash do evento anterior; "
                                     "o último aplicado foi descartado")

    # ------------------------------------------------ consumo

    def consumir(self, maximo=None):
        """Lê até 'maximo' mensagens a partir do checkpoint. Devolve quantos eventos aplicou.

        Um grupo nunca é aplicado pela metade: se o lote acaba no meio de um,
        o consumidor continua lendo até fechá-lo. Se o tópico acaba antes (o
        resto ainda não chegou), o grupo fica pendente, o checkpoint não se move
        e, na próxima chamada, ele é lido de novo, inteiro.
        """
        anterior = self.cabeca[1] if self.mensagens else None
        pendentes = []
        aplicados = 0
        for off, msg in self._mensagens(maximo, lambda: bool(pendentes)):
            if msg.get("tipo") == "referencial":
                if sha256(msg["corpo"]) != msg["ref_id"]:
                    raise TopicoCorrompido(off, "corpo do referencial não confere com o endereço")
                self.refs.setdefault(msg["ref_id"], msg["corpo"])
                if not pendentes:
                    self.offset = off + 1
                continue
            if msg.get("tipo") != "evento":
                raise TopicoCorrompido(off, f"tipo de mensagem desconhecido: {msg.get('tipo')!r}")
            ev = msg["evento"]
            recalculado = hash_do_evento(ev)
            if recalculado != ev["hash"]:
                raise TopicoCorrompido(off, "hash não confere com o conteúdo")
            ja = self._hash_de.get(ev["event_id"]) or next(
                (p["hash"] for p in pendentes if p["event_id"] == ev["event_id"]), None)
            if ja is not None:
                if ja != ev["hash"]:
                    raise TopicoCorrompido(off, "evento repetido com conteúdo diferente")
                if not pendentes:                  # entrega repetida: ignora
                    self.offset = off + 1
                continue
            if ev["prev_hash"] != anterior:
                if pendentes and any(p["hash"] == ev["prev_hash"] for p in pendentes):
                    raise TopicoCorrompido(off, "bifurcação dentro de um grupo ainda não aplicado")
                raise self._elo_quebrado(off, ev)
            anterior = ev["hash"]
            gid, gn = ev.get("grupo_id"), ev.get("grupo_n")
            if gid is None:
                if pendentes:
                    raise TopicoCorrompido(off, "grupo incompleto antes de um evento fora dele")
                self._aplicar([ev], off + 1)
                aplicados += 1
                continue
            inicio = pendentes[0] if pendentes else ev
            if gid != inicio["event_id"] or gn != inicio["grupo_n"]:
                raise TopicoCorrompido(off, "evento não pertence ao grupo em curso")
            pendentes.append(ev)
            if len(pendentes) == gn:
                self._aplicar(pendentes, off + 1)
                aplicados += len(pendentes)
                pendentes = []
        return aplicados

    def _mensagens(self, maximo, grupo_aberto):
        off = self.offset
        for off, msg in self.topico.ler(self.offset, maximo):
            yield off, msg
        if maximo is None:
            return
        while grupo_aberto():
            prox = self.topico.ler(off + 1, 1)
            if not prox:
                return
            off, msg = prox[0]
            yield off, msg

    def em_dia(self):
        """True se não há mensagem nova no tópico depois do checkpoint."""
        return self.offset >= self.topico.fim()

    # ------------------------------------------------ leitura

    def _corte(self, ate):
        n = relogio.limite(ate) if isinstance(ate, datetime) else int(ate)
        if self.cabeca is None or n > self.cabeca[0]:
            raise ConsumidorAtrasado(
                f"{self.nome}: pedido até {n}, recebido até {self.cabeca[0] if self.cabeca else 'nada'}")
        i = bisect.bisect_right(self._fronteiras, n)
        return self._fronteiras[i - 1] if i else None

    def _dobrar(self, fid, n):
        """(feature_type, owner_ref, estado plano) da feature ao fim de n, ou None se não está viva."""
        evs = [e for e in self._por_feature.get(fid, []) if e["event_id"] <= n]
        ciclo = [e for e in evs if e["event_type"] in ("BASELINE", "VOID")]
        if not ciclo or ciclo[-1]["event_type"] == "VOID":
            return None
        b = ciclo[-1]["event_id"]
        estado = {}
        for e in evs:
            if e["event_id"] < b:
                continue
            for c in e["campos"]:
                if c["op"] == "set":
                    estado[c["path"]] = c["next"]
                else:
                    estado.pop(c["path"], None)
        return evs[0]["feature_type"], evs[0]["owner_ref"], estado

    def _fecho(self, vivas):
        """Fecho de dependência das raízes sobre as arestas declaradas.

        Aresta declarada: owner_ref, ou um valor de campo igual ao id de outra
        feature viva (adc.pista, adc.aplica_a.pista...), nos dois sentidos.
        Quem tem seed depende do referencial: o airport entra no fecho, mas não
        é expandido (senão tudo que tem posição entraria).
        """
        refs = {}
        for fid, (_, owner, est) in vivas.items():
            r = {v for v in est.values() if isinstance(v, str) and v in vivas and v != fid}
            if owner in vivas:
                r.add(owner)
            refs[fid] = r
        vizinhos = {fid: set(r) for fid, r in refs.items()}
        for fid, r in refs.items():
            for alvo in r:
                vizinhos[alvo].add(fid)
        aeros = {fid for fid, (tp, _, _) in vivas.items() if tp == "airport"}
        fila = [f for f in (self.raizes or sorted(vivas)) if f in vivas]
        fecho = set(fila)
        while fila:
            x = fila.pop()
            if x in aeros and x not in (self.raizes or ()):
                continue
            proximos = set(vizinhos[x])
            if any(p.endswith(".referente") for p in vivas[x][2]):
                proximos |= aeros
            for y in proximos - fecho:
                fecho.add(y)
                fila.append(y)
        return sorted(fecho)

    def ler(self, ate):
        """Estado e derivados do fecho ao fim de 'ate' (event_id ou instante), como dado."""
        n = self._corte(ate)
        vivas = {}
        if n is not None:
            for fid in sorted(self._por_feature):
                d = self._dobrar(fid, n)
                if d is not None:
                    vivas[fid] = d
        fecho = self._fecho(vivas)
        estados = {fid: {"tipo": vivas[fid][0], "estado": aninhar(vivas[fid][2])} for fid in fecho}

        aeros = [fid for fid in fecho if vivas[fid][0] == "airport"]
        ref_id = vivas[aeros[0]][2].get(CAMPO_REFERENCIAL) if len(aeros) == 1 else None
        if ref_id is None:
            derivados = {"recusado": "sem referencial vigente"}
        elif ref_id not in self.refs:
            derivados = {"recusado": "corpo do referencial não recebido"}
        else:
            corpo = self.refs[ref_id]
            derivados = {"comprimento": {el: derivacoes.comprimento(corpo, el)
                                         for el in sorted(corpo["elementos_lineares"])}}
            camadas = tuple(c + "." for c in derivacoes.CAMADAS_DERIVACAO)
            entrada = {fid: (vivas[fid][0], aninhar({p: v for p, v in vivas[fid][2].items() if p.startswith(camadas)}))
                       for fid in fecho if vivas[fid][0] in TIPOS_DISTANCIAS}
            try:
                derivados["distancias"] = derivacoes.distancias_de(corpo, entrada)
            except derivacoes.RegraPendente as exc:
                derivados["distancias"] = {"recusado": str(exc)}

        resultado = {
            "run_id": self.mensagens[0]["run_id"],
            "ate": n,
            "cabeca": self._hash_de[n] if n is not None else None,
            "raizes": self.raizes,
            "fecho": fecho,
            "estados": estados,
            "referencial": ref_id,
            "derivados": derivados,
            "regra": _regra(),
        }
        resultado["hash_derivados"] = sha256(derivados)
        resultado["hash"] = sha256(resultado)
        return resultado

    # ------------------------------------------------ checkpoint

    def salvar(self, caminho):
        """Grava o checkpoint e o log local (o que sobrevive a uma queda)."""
        Path(caminho).write_text(json.dumps({
            "formato": 1, "nome": self.nome, "raizes": self.raizes, "offset": self.offset,
            "mensagens": self.mensagens, "refs": self.refs}, ensure_ascii=False))
        return caminho

    @classmethod
    def carregar(cls, caminho, topico):
        """Retoma de um checkpoint. O log local é conferido de novo (hash e elo) antes de valer."""
        d = json.loads(Path(caminho).read_text())
        c = cls(topico, d["raizes"], d["nome"])
        local = TopicoMemoria()
        for ref_id, corpo in d["refs"].items():
            local.anexar({"tipo": "referencial", "ref_id": ref_id, "corpo": corpo})
        for ev in d["mensagens"]:
            local.anexar({"tipo": "evento", "evento": ev})
        c.topico = local
        c.consumir()
        c.topico, c.offset = topico, d["offset"]
        return c
