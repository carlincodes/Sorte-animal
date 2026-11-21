from __future__ import annotations

import argparse
import asyncio
import logging
import time
from collections import OrderedDict

from middleware.protocol import codificar, decodificar, decidir_publicacao, mensagem

LOG = logging.getLogger("broker")


class Broker:
    def __init__(
        self,
        nome: str,
        par: tuple[str, int],
        limite_topico: int = 200,
        limite_cliente: int = 100,
        atraso_entrega: float = 0.0,
    ):
        self.nome = nome
        self.par = par
        self.limite_topico = limite_topico
        self.limite_cliente = limite_cliente
        self.atraso_entrega = atraso_entrega
        self.transport: asyncio.DatagramTransport | None = None
        self.topicos: set[str] = set()
        self.filas_topico: dict[str, asyncio.Queue] = {}
        self.inscritos: dict[str, dict[str, tuple]] = {}
        self.clientes: dict[str, tuple] = {}
        self.saidas: dict[str, asyncio.Queue] = {}
        self.interesse_remoto: dict[str, int] = {}
        self.interesse_ts = 0.0
        self.marco_topico: dict[str, float] = {}
        self.apagados: dict[str, float] = {}
        self.em_distribuicao: dict[str, int] = {}
        self.em_voo: dict[str, int] = {}
        self.vistos: OrderedDict[str, float] = OrderedDict()
        self.par_visto_em = 0.0
        self.iniciado = time.time()
        self.contadores = {
            "recebidas": 0,
            "aceitas": 0,
            "descartadas": 0,
            "rejeitadas": 0,
            "duplicadas": 0,
            "entregas": 0,
        }

    @property
    def par_vivo(self) -> bool:
        return (time.time() - self.par_visto_em) < 3.0

    def enviar(self, addr, msg: dict) -> None:
        if self.transport is None or addr is None:
            return
        dados = codificar(msg)
        if len(dados) > 60000:
            return
        self.transport.sendto(dados, addr)

    def confirmar(self, addr, id_msg: str, status: str, extra: dict | None = None) -> None:
        payload = {"status": status}
        if extra:
            payload.update(extra)
        self.enviar(addr, mensagem("ack", id=id_msg, payload=payload))

    def erro(self, addr, id_msg: str | None, codigo: str, detalhe: str) -> None:
        if not id_msg:
            return
        self.enviar(
            addr,
            mensagem("error", id=id_msg, payload={"codigo": codigo, "detalhe": detalhe}),
        )

    def ao_receber(self, dados: bytes, addr) -> None:
        try:
            msg = decodificar(dados)
        except (ValueError, UnicodeError, KeyError):
            return
        asyncio.create_task(self.tratar(msg, addr))

    async def tratar(self, msg: dict, addr) -> None:
        tipo = msg["type"]
        self.contadores["recebidas"] += 1
        if tipo == "ping":
            self.enviar(addr, mensagem("pong", id=msg["id"]))
            return
        if tipo == "stats":
            self.enviar(addr, mensagem("ack", id=msg["id"], payload=self.retrato()))
            return
        if tipo == "create_topic":
            status = self.criar_topico(msg.get("topic"), avisar_par=True)
            if status == "invalid":
                self.erro(addr, msg["id"], "mensagem_invalida", "Informe o nome do tópico")
            else:
                self.confirmar(addr, msg["id"], status)
            return
        if tipo == "delete_topic":
            if not msg.get("topic"):
                self.erro(addr, msg["id"], "mensagem_invalida", "Informe o nome do tópico")
                return
            self.excluir_topico(msg.get("topic"), avisar_par=True)
            self.confirmar(addr, msg["id"], "deleted")
            return
        if tipo == "subscribe":
            self.inscrever(msg, addr)
            return
        if tipo == "unsubscribe":
            self.cancelar(msg, addr)
            return
        if tipo == "publish":
            await self.aceitar_publicacao(msg, addr, do_par=False)
            return
        if tipo == "broker_publish":
            self.par_visto_em = time.time()
            await self.aceitar_publicacao(msg, addr, do_par=True)
            return
        if tipo == "broker_hello":
            self.receber_hello(msg)
            return
        if tipo == "broker_topic":
            self.par_visto_em = time.time()
            self.receber_topico(msg)
            return
        if tipo == "broker_interest":
            self.par_visto_em = time.time()
            self.atualizar_interesse(msg.get("ts") or 0, (msg.get("payload") or {}).get("counts") or {})
            return
        self.erro(addr, msg.get("id"), "mensagem_invalida", f"tipo desconhecido: {tipo}")

    def criar_topico(self, topic: str | None, avisar_par: bool, quando: float | None = None) -> str:
        if not topic:
            return "invalid"
        if topic in self.topicos:
            return "exists"
        quando = time.time() if quando is None else quando
        self.apagados.pop(topic, None)
        self.marco_topico[topic] = quando
        if topic not in self.filas_topico:
            self.filas_topico[topic] = asyncio.Queue(maxsize=self.limite_topico)
            asyncio.create_task(self.distribuir(topic))
        self.topicos.add(topic)
        self.inscritos.setdefault(topic, {})
        if avisar_par:
            self.enviar(
                self.par,
                mensagem(
                    "broker_topic",
                    topic=topic,
                    payload={"op": "create", "ts": quando},
                    origem=self.nome,
                ),
            )
        LOG.info("[%s] tópico criado: %s", self.nome, topic)
        return "created"

    def excluir_topico(self, topic: str | None, avisar_par: bool, quando: float | None = None) -> None:
        if not topic:
            return
        quando = time.time() if quando is None else quando
        self.marco_topico[topic] = quando
        self.apagados[topic] = quando
        if len(self.apagados) > 200:
            antigo = min(self.apagados, key=self.apagados.get)
            self.apagados.pop(antigo, None)
            self.marco_topico.pop(antigo, None)
        self.topicos.discard(topic)
        self.inscritos.pop(topic, None)
        if avisar_par:
            self.enviar(
                self.par,
                mensagem(
                    "broker_topic",
                    topic=topic,
                    payload={"op": "delete", "ts": quando},
                    origem=self.nome,
                ),
            )
        LOG.info("[%s] tópico excluído: %s", self.nome, topic)

    def receber_topico(self, msg: dict) -> None:
        topic = msg.get("topic")
        payload = msg.get("payload") or {}
        quando = float(payload.get("ts") or msg.get("ts") or time.time())
        if quando < self.marco_topico.get(topic, 0):
            return
        if payload.get("op") == "delete":
            self.excluir_topico(topic, avisar_par=False, quando=quando)
        elif topic not in self.topicos:
            self.criar_topico(topic, avisar_par=False, quando=quando)
        else:
            self.marco_topico[topic] = quando
            self.apagados.pop(topic, None)

    def atualizar_interesse(self, ts: float, counts: dict) -> None:
        if ts < self.interesse_ts:
            return
        self.interesse_ts = ts
        self.interesse_remoto = {k: int(v) for k, v in counts.items()}

    def receber_hello(self, msg: dict) -> None:
        self.par_visto_em = time.time()
        payload = msg.get("payload") or {}
        self.atualizar_interesse(msg.get("ts") or 0, payload.get("counts") or {})
        for topic, ts in (payload.get("apagados") or {}).items():
            ts = float(ts)
            if ts >= self.marco_topico.get(topic, 0) and topic in self.topicos:
                self.excluir_topico(topic, avisar_par=False, quando=ts)
        for topic, ts in (payload.get("topics") or {}).items():
            ts = float(ts)
            if ts < self.marco_topico.get(topic, 0) or ts < self.apagados.get(topic, 0):
                continue
            if topic not in self.topicos:
                self.criar_topico(topic, avisar_par=False, quando=ts)

    def garantir_saida(self, client_id: str, addr) -> asyncio.Queue:
        self.clientes[client_id] = addr
        fila = self.saidas.get(client_id)
        if fila is None:
            fila = asyncio.Queue(maxsize=self.limite_cliente)
            self.saidas[client_id] = fila
            self.em_voo[client_id] = 0
            asyncio.create_task(self.enviar_para_cliente(client_id))
        else:
            self.clientes[client_id] = addr
        return fila

    def inscrever(self, msg: dict, addr) -> None:
        topic = msg.get("topic")
        client_id = msg.get("client_id")
        if not topic or not client_id:
            self.erro(addr, msg.get("id"), "mensagem_invalida", "topic e client_id são obrigatórios")
            return
        if topic not in self.topicos:
            self.erro(addr, msg["id"], "topico_inexistente", f"O tópico {topic} não existe")
            return
        self.garantir_saida(client_id, addr)
        self.inscritos.setdefault(topic, {})[client_id] = addr
        self.anunciar_interesse()
        self.confirmar(addr, msg["id"], "subscribed", {"inscritos": len(self.inscritos[topic])})
        LOG.info("[%s] %s inscrito em %s", self.nome, client_id, topic)

    def cancelar(self, msg: dict, addr) -> None:
        topic = msg.get("topic")
        client_id = msg.get("client_id")
        if topic and client_id:
            self.inscritos.get(topic, {}).pop(client_id, None)
            self.anunciar_interesse()
        self.confirmar(addr, msg["id"], "unsubscribed")

    def anunciar_interesse(self) -> None:
        counts = {topic: len(subs) for topic, subs in self.inscritos.items() if subs}
        aviso = mensagem("broker_interest", payload={"counts": counts}, origem=self.nome)
        self.enviar(self.par, aviso)

    def ja_vista(self, id_msg: str) -> bool:
        if id_msg in self.vistos:
            self.vistos.move_to_end(id_msg)
            return True
        return False

    def lembrar(self, id_msg: str) -> None:
        self.vistos[id_msg] = time.time()
        self.vistos.move_to_end(id_msg)
        while len(self.vistos) > 5000:
            self.vistos.popitem(last=False)

    def fila_sem_vaga(self, topic: str, locais: dict) -> bool:
        fila = self.filas_topico.get(topic)
        if fila is None or fila.full():
            return True
        espera = fila.qsize() + self.em_distribuicao.get(topic, 0)
        for client_id in locais:
            saida = self.saidas.get(client_id)
            if saida is None:
                return True
            ocupado = saida.qsize() + self.em_voo.get(client_id, 0)
            if ocupado + espera >= self.limite_cliente:
                return True
        return False

    async def aceitar_publicacao(self, msg: dict, addr, do_par: bool) -> None:
        topic = msg.get("topic") or ""
        id_msg = msg["id"]
        locais = self.inscritos.get(topic, {}) if topic in self.topicos else {}
        remotos = self.interesse_remoto.get(topic, 0) > 0 and self.par_vivo and not do_par
        fila = self.filas_topico.get(topic)
        decisao = decidir_publicacao(
            duplicada=self.ja_vista(id_msg),
            topico_existe=topic in self.topicos,
            tem_inscritos=bool(locais) or remotos,
            fila_topico_cheia=fila is not None and fila.full(),
            fila_cliente_cheia=bool(locais) and self.fila_sem_vaga(topic, locais),
        )
        if decisao == "duplicate":
            self.contadores["duplicadas"] += 1
            if not do_par:
                self.confirmar(addr, id_msg, "duplicate")
            return
        if decisao == "topico_inexistente":
            if not do_par:
                self.erro(addr, id_msg, "topico_inexistente", f"O tópico {topic} não existe")
            return
        if decisao == "discarded_no_subscribers":
            self.lembrar(id_msg)
            self.contadores["descartadas"] += 1
            LOG.info("[%s] %s descartada, sem inscritos", self.nome, topic)
            if not do_par:
                self.confirmar(addr, id_msg, "discarded_no_subscribers", {"inscritos": 0})
            return
        if decisao == "fila_cheia":
            self.contadores["rejeitadas"] += 1
            LOG.warning("[%s] publicação rejeitada, fila cheia em %s", self.nome, topic)
            if not do_par:
                self.erro(
                    addr,
                    id_msg,
                    "fila_cheia",
                    "Fila cheia. A publicação foi rejeitada para não descartar mensagem já aceita.",
                )
            return

        self.lembrar(id_msg)
        self.filas_topico[topic].put_nowait(
            {
                "id": id_msg,
                "topic": topic,
                "payload": msg.get("payload") if isinstance(msg.get("payload"), dict) else {},
                "do_par": do_par,
                "client_id": msg.get("client_id"),
            }
        )
        self.contadores["aceitas"] += 1
        if not do_par:
            self.confirmar(
                addr,
                id_msg,
                "accepted",
                {"inscritos_locais": len(locais), "encaminhado": remotos},
            )

    async def distribuir(self, topic: str) -> None:
        fila = self.filas_topico[topic]
        while True:
            item = await fila.get()
            self.em_distribuicao[topic] = self.em_distribuicao.get(topic, 0) + 1
            try:
                if topic not in self.topicos:
                    continue
                entregues = 0
                for client_id in list(self.inscritos.get(topic, {})):
                    saida = self.saidas.get(client_id)
                    if saida is None or saida.full():
                        LOG.warning("[%s] sem vaga na fila de %s", self.nome, client_id)
                        continue
                    entrega = mensagem(
                        "deliver",
                        topic=topic,
                        id=item["id"],
                        payload=item["payload"],
                        client_id=item.get("client_id"),
                        via=self.nome,
                    )
                    saida.put_nowait(entrega)
                    entregues += 1
                    self.contadores["entregas"] += 1
                if not item["do_par"] and self.interesse_remoto.get(topic, 0) > 0 and self.par_vivo:
                    self.enviar(
                        self.par,
                        mensagem(
                            "broker_publish",
                            topic=topic,
                            id=item["id"],
                            payload=item["payload"],
                            client_id=item.get("client_id"),
                            origem=self.nome,
                        ),
                    )
                    LOG.info("[%s] encaminhou %s (%s locais)", self.nome, topic, entregues)
                else:
                    LOG.info("[%s] entregou %s a %s assinante(s)", self.nome, topic, entregues)
            finally:
                self.em_distribuicao[topic] -= 1
                fila.task_done()

    async def enviar_para_cliente(self, client_id: str) -> None:
        fila = self.saidas[client_id]
        while True:
            msg = await fila.get()
            self.em_voo[client_id] = self.em_voo.get(client_id, 0) + 1
            try:
                if self.atraso_entrega:
                    await asyncio.sleep(self.atraso_entrega)
                addr = self.clientes.get(client_id)
                if addr is not None:
                    self.enviar(addr, msg)
            finally:
                self.em_voo[client_id] -= 1
                fila.task_done()

    def retrato(self) -> dict:
        topicos = []
        for topic in sorted(self.topicos):
            fila = self.filas_topico.get(topic)
            topicos.append(
                {
                    "nome": topic,
                    "locais": len(self.inscritos.get(topic, {})),
                    "remotos": self.interesse_remoto.get(topic, 0),
                    "fila": fila.qsize() if fila else 0,
                }
            )
        return {
            "nome": self.nome,
            "par": f"{self.par[0]}:{self.par[1]}",
            "par_vivo": self.par_vivo,
            "uptime": round(time.time() - self.iniciado, 1),
            "clientes": len(self.clientes),
            "topicos": topicos,
            "contadores": dict(self.contadores),
            "limites": {"topico": self.limite_topico, "cliente": self.limite_cliente},
        }

    async def pulsar(self) -> None:
        while True:
            agora = time.time()
            velhos = [chave for chave, visto in self.vistos.items() if agora - visto > 120]
            for chave in velhos:
                self.vistos.pop(chave, None)
            counts = {topic: len(subs) for topic, subs in self.inscritos.items() if subs}
            self.enviar(
                self.par,
                mensagem(
                    "broker_hello",
                    payload={
                        "topics": {topic: self.marco_topico.get(topic, self.iniciado) for topic in self.topicos},
                        "apagados": dict(self.apagados),
                        "counts": counts,
                    },
                    origem=self.nome,
                ),
            )
            await asyncio.sleep(1)


class Protocolo(asyncio.DatagramProtocol):
    def __init__(self, broker: Broker):
        self.broker = broker

    def connection_made(self, transport):
        self.broker.transport = transport

    def datagram_received(self, data, addr):
        self.broker.ao_receber(data, addr)

    def error_received(self, exc):
        LOG.warning("[%s] erro de socket: %s", self.broker.nome, exc)


async def servir(nome, porta, par, limite_topico, limite_cliente, atraso_entrega):
    broker = Broker(nome, par, limite_topico, limite_cliente, atraso_entrega)
    loop = asyncio.get_running_loop()
    await loop.create_datagram_endpoint(lambda: Protocolo(broker), local_addr=("127.0.0.1", porta))
    LOG.info("[%s] escutando udp://127.0.0.1:%s par=%s:%s", nome, porta, par[0], par[1])
    await broker.pulsar()


def main():
    parser = argparse.ArgumentParser(description="Broker publish/subscribe em UDP")
    parser.add_argument("--nome", default="broker-a")
    parser.add_argument("--porta", type=int, default=9001)
    parser.add_argument("--par", default="127.0.0.1:9002")
    parser.add_argument("--fila-topico", type=int, default=200)
    parser.add_argument("--fila-cliente", type=int, default=100)
    parser.add_argument("--atraso-ms", type=int, default=0, help="atraso da entrega ao cliente, em milissegundos")
    args = parser.parse_args()
    host, porta_par = args.par.split(":")
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s", datefmt="%H:%M:%S")
    try:
        asyncio.run(
            servir(
                args.nome,
                args.porta,
                (host, int(porta_par)),
                args.fila_topico,
                args.fila_cliente,
                args.atraso_ms / 1000,
            )
        )
    except KeyboardInterrupt:
        LOG.info("[%s] encerrado", args.nome)


if __name__ == "__main__":
    main()
