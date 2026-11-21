from __future__ import annotations

import asyncio
import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor

from middleware.protocol import codificar, decodificar, mensagem, novo_id

LOG = logging.getLogger("cliente")


class ErroMiddleware(RuntimeError):
    def __init__(self, detalhe: str, resposta: dict | None = None):
        super().__init__(detalhe)
        self.resposta = resposta or {}


class _Protocolo(asyncio.DatagramProtocol):
    def __init__(self, cliente: "ClienteMiddleware"):
        self.cliente = cliente

    def datagram_received(self, data, addr):
        try:
            msg = decodificar(data)
        except (ValueError, UnicodeError):
            return
        self.cliente.ao_receber(msg)

    def error_received(self, exc):
        LOG.warning("erro de socket no cliente: %s", exc)


class ClienteMiddleware:
    def __init__(
        self,
        brokers: list[tuple[str, int]],
        client_id: str | None = None,
        timeout: float = 0.8,
        indice_inicial: int = 0,
        intervalo_ping: float = 1.0,
    ):
        if not brokers:
            raise ValueError("informe ao menos um broker")
        self.brokers = brokers
        self.client_id = client_id or f"cliente-{novo_id()[:8]}"
        self.timeout = timeout
        self.intervalo_ping = intervalo_ping
        self._ativo = indice_inicial % len(brokers)
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._transport = None
        self._prontos = threading.Event()
        self._parar = False
        self._pendentes: dict[str, asyncio.Future] = {}
        self._inscritos: dict[str, callable] = {}
        self._executor = ThreadPoolExecutor(max_workers=4, thread_name_prefix="entrega")
        self._trava_troca: asyncio.Lock | None = None
        self.conectado = False
        self.broker_ativo = self._endereco(self._ativo)

    def _endereco(self, indice: int) -> str:
        host, porta = self.brokers[indice]
        return f"{host}:{porta}"

    def iniciar(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._thread = threading.Thread(target=self._rodar, name=f"pubsub-{self.client_id}", daemon=True)
        self._thread.start()
        if not self._prontos.wait(5):
            raise ErroMiddleware("a biblioteca cliente não abriu o socket UDP")

    def _rodar(self) -> None:
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        self._trava_troca = asyncio.Lock()
        try:
            self._loop.run_until_complete(self._principal())
        finally:
            self._loop.close()

    async def _principal(self) -> None:
        loop = asyncio.get_running_loop()
        self._transport, _ = await loop.create_datagram_endpoint(
            lambda: _Protocolo(self),
            local_addr=("127.0.0.1", 0),
        )
        self._prontos.set()
        await self._vigiar()

    async def _vigiar(self) -> None:
        while not self._parar:
            if not await self._ping(self.brokers[self._ativo], self.timeout):
                await self._trocar_broker()
            else:
                self.conectado = True
                self.broker_ativo = self._endereco(self._ativo)
            await asyncio.sleep(self.intervalo_ping)

    def encerrar(self) -> None:
        self._parar = True
        if self._loop and self._transport:
            self._loop.call_soon_threadsafe(self._transport.close)
        self._executor.shutdown(wait=False, cancel_futures=True)

    def ao_receber(self, msg: dict) -> None:
        tipo = msg.get("type")
        if tipo in {"ack", "error", "pong"}:
            futuro = self._pendentes.get(msg.get("id", ""))
            if futuro and not futuro.done():
                futuro.set_result(msg)
            return
        if tipo == "deliver":
            callback = self._inscritos.get(msg.get("topic", ""))
            if callback:
                self._executor.submit(self._entregar, callback, msg)

    @staticmethod
    def _entregar(callback, msg: dict) -> None:
        try:
            callback(msg)
        except Exception:
            LOG.exception("falha no callback de %s", msg.get("topic"))

    async def _enviar_e_esperar(self, broker: tuple[str, int], msg: dict, timeout: float) -> dict:
        loop = asyncio.get_running_loop()
        futuro = loop.create_future()
        self._pendentes[msg["id"]] = futuro
        self._transport.sendto(codificar(msg), broker)
        try:
            return await asyncio.wait_for(futuro, timeout)
        finally:
            self._pendentes.pop(msg["id"], None)

    async def _ping(self, broker: tuple[str, int], timeout: float) -> bool:
        try:
            resp = await self._enviar_e_esperar(
                broker,
                mensagem("ping", client_id=self.client_id),
                timeout,
            )
        except (TimeoutError, asyncio.TimeoutError, OSError):
            return False
        return resp.get("type") == "pong"

    async def _trocar_broker(self) -> bool:
        assert self._trava_troca is not None
        async with self._trava_troca:
            if await self._ping(self.brokers[self._ativo], self.timeout):
                self.conectado = True
                return False
            antigo = self.brokers[self._ativo]
            for indice, broker in enumerate(self.brokers):
                if indice == self._ativo:
                    continue
                if not await self._ping(broker, self.timeout):
                    continue
                for topic in list(self._inscritos):
                    pedido = mensagem("unsubscribe", topic=topic, client_id=self.client_id)
                    try:
                        await self._enviar_e_esperar(antigo, pedido, 0.25)
                    except (TimeoutError, asyncio.TimeoutError):
                        break
                self._ativo = indice
                await self._refazer_inscricoes()
                self.broker_ativo = self._endereco(indice)
                self.conectado = True
                LOG.warning("%s passou a usar %s", self.client_id, self.broker_ativo)
                return True
            self.conectado = False
            return False

    async def _refazer_inscricoes(self) -> None:
        broker = self.brokers[self._ativo]
        for topic in list(self._inscritos):
            try:
                await self._enviar_e_esperar(
                    broker,
                    mensagem("create_topic", topic=topic, client_id=self.client_id),
                    self.timeout,
                )
                await self._enviar_e_esperar(
                    broker,
                    mensagem("subscribe", topic=topic, client_id=self.client_id),
                    self.timeout,
                )
            except (TimeoutError, asyncio.TimeoutError):
                LOG.warning("não foi possível reinscrever %s", topic)

    async def _solicitar(self, tipo: str, topic: str | None, payload: dict | None) -> dict:
        msg = mensagem(tipo, topic=topic, payload=payload, client_id=self.client_id)
        broker = self.brokers[self._ativo]
        for _ in range(2):
            try:
                resp = await self._enviar_e_esperar(broker, msg, self.timeout)
                self.conectado = True
                self.broker_ativo = self._endereco(self._ativo)
                return resp
            except (TimeoutError, asyncio.TimeoutError):
                continue
        if await self._ping(broker, self.timeout):
            raise ErroMiddleware("o broker respondeu ao ping, mas não confirmou a operação")
        await self._trocar_broker()
        if not self.conectado:
            raise ErroMiddleware("nenhum broker está disponível")
        msg["id"] = novo_id()
        try:
            return await self._enviar_e_esperar(self.brokers[self._ativo], msg, self.timeout)
        except (TimeoutError, asyncio.TimeoutError) as exc:
            raise ErroMiddleware("sem confirmação do broker reserva") from exc

    def _chamar(self, coro, timeout: float | None = None) -> dict:
        if self._loop is None:
            raise ErroMiddleware("cliente ainda não foi iniciado")
        futuro = asyncio.run_coroutine_threadsafe(coro, self._loop)
        espera = (timeout or self.timeout) * len(self.brokers) * 4 + 2
        return futuro.result(espera)

    def criar_topico(self, topic: str) -> dict:
        return self._chamar(self._solicitar("create_topic", topic, None))

    def excluir_topico(self, topic: str) -> dict:
        self._inscritos.pop(topic, None)
        return self._chamar(self._solicitar("delete_topic", topic, None))

    def publicar(self, topic: str, payload: dict) -> dict:
        return self._chamar(self._solicitar("publish", topic, payload))

    def inscrever(self, topic: str, callback) -> dict:
        self._inscritos[topic] = callback
        try:
            resp = self._chamar(self._solicitar("subscribe", topic, None))
        except Exception:
            self._inscritos.pop(topic, None)
            raise
        if resp.get("type") == "error":
            self._inscritos.pop(topic, None)
            detalhe = (resp.get("payload") or {}).get("detalhe") or "falha na inscrição"
            raise ErroMiddleware(detalhe, resp)
        return resp

    def cancelar_inscricao(self, topic: str) -> dict:
        self._inscritos.pop(topic, None)
        return self._chamar(self._solicitar("unsubscribe", topic, None))

    def consultar(self, indice: int, timeout: float | None = None) -> dict:
        broker = self.brokers[indice]
        endereco = self._endereco(indice)
        timeout = timeout or self.timeout

        async def pedido():
            return await self._enviar_e_esperar(
                broker,
                mensagem("stats", client_id=self.client_id),
                timeout,
            )

        try:
            resp = self._chamar(pedido(), timeout)
        except Exception:
            return {"online": False, "endereco": endereco, "nome": endereco}
        payload = dict(resp.get("payload") or {})
        payload["online"] = True
        payload["endereco"] = endereco
        return payload

    def aguardar_conexao(self, segundos: float = 5) -> bool:
        limite = time.time() + segundos
        while time.time() < limite:
            if self.conectado:
                return True
            time.sleep(0.05)
        return self.conectado
