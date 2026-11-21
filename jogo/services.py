from __future__ import annotations

import logging
import threading
import time
from collections import deque

from django.db import IntegrityError, close_old_connections, transaction
from django.utils import timezone

from jogo.models import Aposta, Evento, Jogada, Jogador, Opcao
from middleware.client import ClienteMiddleware, ErroMiddleware

LOG = logging.getLogger("jogo")

TOPICOS = ("jogo/rodada", "jogo/palpites", "jogo/resultado", "jogo/placar", "demo/teste")
BROKERS = [("127.0.0.1", 9001), ("127.0.0.1", 9002)]

cliente: ClienteMiddleware | None = None
historico: deque = deque(maxlen=12)
_trava_jogo = threading.Lock()
_trava_hist = threading.Lock()
_inicio = False


def iniciar() -> None:
    global cliente, _inicio
    if _inicio:
        return
    _inicio = True
    cliente = ClienteMiddleware(BROKERS, client_id="aplicacao-jogo")
    cliente.iniciar()
    threading.Thread(target=_preparar, name="inscricoes", daemon=True).start()
    LOG.info("cliente do jogo iniciado; aguardando os brokers")


def _preparar() -> None:
    assert cliente is not None
    pendentes = set(TOPICOS)
    while pendentes:
        for topic in list(pendentes):
            try:
                criado = cliente.criar_topico(topic)
                if criado.get("type") == "error":
                    continue
                cliente.inscrever(topic, despachar)
            except Exception:
                LOG.info("ainda não inscrevi %s; nova tentativa", topic)
                time.sleep(0.4)
                continue
            pendentes.discard(topic)
            LOG.info("inscrito em %s via %s", topic, cliente.broker_ativo)
        if pendentes:
            time.sleep(0.4)


def publicar(topic: str, payload: dict) -> dict:
    if cliente is None:
        raise ErroMiddleware("o middleware da aplicação ainda não iniciou")
    resposta = cliente.publicar(topic, payload)
    codigo = (resposta.get("payload") or {}).get("codigo")
    if resposta.get("type") == "error" and codigo == "topico_inexistente":
        cliente.criar_topico(topic)
        cliente.inscrever(topic, despachar)
        resposta = cliente.publicar(topic, payload)
    with _trava_hist:
        historico.appendleft(
            {
                "hora": timezone.localtime().strftime("%H:%M:%S"),
                "topic": topic,
                "pedido": payload,
                "resposta": resposta,
            }
        )
    return resposta


def copiar_historico() -> list:
    with _trava_hist:
        return list(historico)


def interpretar(resposta: dict) -> tuple[bool, str]:
    if resposta.get("type") == "error":
        payload = resposta.get("payload") or {}
        return False, payload.get("detalhe") or payload.get("codigo") or "erro do middleware"
    status = (resposta.get("payload") or {}).get("status")
    if status == "discarded_no_subscribers":
        return False, "O broker descartou a mensagem porque o tópico não tem inscritos."
    if status in {"accepted", "duplicate"}:
        return True, status
    return False, status or "resposta inesperada do broker"


def despachar(msg: dict) -> None:
    close_old_connections()
    try:
        with _trava_jogo:
            _aplicar(msg)
    except Exception:
        LOG.exception("falha ao aplicar mensagem %s", msg.get("id"))
    finally:
        close_old_connections()


def _aplicar(msg: dict) -> None:
    topic = msg.get("topic")
    payload = msg.get("payload") or {}
    mid = msg.get("id") or ""
    if topic == "jogo/rodada":
        _rodada(payload, msg)
    elif topic == "jogo/palpites":
        _palpite(payload, mid, msg)
    elif topic == "jogo/resultado":
        _resultado(payload, mid, msg)
    elif topic == "jogo/placar":
        _registrar("jogo/placar", "Placar distribuído aos inscritos.", msg)
    elif topic == "demo/teste":
        texto = payload.get("texto") or "mensagem de teste"
        _registrar("demo/teste", f"Entrega recebida: {texto}", msg)


PONTOS_ACERTO = 10


def _rodada(payload: dict, msg: dict) -> None:
    if payload.get("evento") != "aberta":
        _registrar("jogo/rodada", "Aviso de jogada recebido.", msg)
        return
    jogada = Jogada.objects.filter(pk=payload.get("jogada_id"), sala__codigo=payload.get("sala")).first()
    if jogada is None:
        _registrar("jogo/rodada", "Abertura ignorada: jogada desconhecida.", msg)
        return
    if jogada.status != "rascunho":
        _registrar("jogo/rodada", f"Jogada {jogada.ordem} já estava {jogada.status}.", msg)
        return
    if Jogada.objects.filter(sala=jogada.sala, status="aberta").exclude(pk=jogada.pk).exists():
        _registrar("jogo/rodada", "Abertura ignorada: outra jogada desta sala já está aberta.", msg)
        return
    jogada.status = "aberta"
    jogada.aberta_em = timezone.now()
    jogada.save(update_fields=["status", "aberta_em"])
    _registrar("jogo/rodada", f"Sala {jogada.sala.codigo}: jogada {jogada.ordem} aberta.", msg)


def _palpite(payload: dict, mid: str, msg: dict) -> None:
    jogada = Jogada.objects.filter(pk=payload.get("jogada_id"), sala__codigo=payload.get("sala"), status="aberta").first()
    if jogada is None:
        _registrar("jogo/palpites", "Palpite ignorado: não há jogada aberta.", msg)
        return
    jogador = Jogador.objects.filter(pk=payload.get("jogador_id"), sala=jogada.sala).first()
    opcao = Opcao.objects.filter(pk=payload.get("opcao_id"), jogada=jogada).first()
    if jogador is None or opcao is None:
        _registrar("jogo/palpites", "Palpite ignorado: jogador ou animal inválido.", msg)
        return
    try:
        with transaction.atomic():
            Aposta.objects.create(jogada=jogada, jogador=jogador, opcao=opcao, mensagem_id=mid)
    except IntegrityError:
        _registrar("jogo/palpites", f"Palpite repetido de {jogador.nome} foi ignorado.", msg)
        return
    _registrar("jogo/palpites", f"{jogador.nome} escolheu {opcao.nome}.", msg)


def _resultado(payload: dict, mid: str, msg: dict) -> None:
    if payload.get("evento") != "divulgar":
        return
    with transaction.atomic():
        jogada = (
            Jogada.objects.select_for_update()
            .filter(pk=payload.get("jogada_id"), sala__codigo=payload.get("sala"), status="aberta")
            .first()
        )
        if jogada is None:
            _registrar("jogo/resultado", "Resultado ignorado: a jogada já estava encerrada.", msg)
            return
        opcao = Opcao.objects.filter(pk=payload.get("opcao_id"), jogada=jogada).first()
        if opcao is None:
            _registrar("jogo/resultado", "Resultado ignorado: animal sorteado não está nesta jogada.", msg)
            return
        jogada.status = "encerrada"
        jogada.animal_sorteado = opcao.nome
        jogada.emoji_sorteado = opcao.emoji
        jogada.imagem_sorteada = opcao.imagem
        jogada.encerrada_em = timezone.now()
        jogada.save()
        ranking = []
        for aposta in jogada.apostas.select_related("jogador"):
            acertou = aposta.opcao_id == opcao.id
            aposta.acertou = acertou
            aposta.pontos = PONTOS_ACERTO if acertou else 0
            aposta.save(update_fields=["acertou", "pontos"])
            if acertou:
                jogador = aposta.jogador
                jogador.pontos += PONTOS_ACERTO
                jogador.save(update_fields=["pontos"])
            ranking.append({"nome": aposta.jogador.nome, "animal": aposta.opcao.nome, "delta": aposta.pontos})
    _registrar("jogo/resultado", f"Sala {jogada.sala.codigo}, jogada {jogada.ordem}: {jogada.animal_sorteado}.", msg)
    if cliente is not None:
        try:
            cliente.publicar(
                "jogo/placar",
                {"sala": jogada.sala.codigo, "jogada": jogada.ordem, "animal": jogada.animal_sorteado, "ranking": ranking},
            )
        except Exception:
            LOG.exception("não foi possível publicar o placar")


def _registrar(topico: str, resumo: str, msg: dict) -> None:
    Evento.objects.create(topico=topico, resumo=resumo, bruto=msg)
    sobra = list(Evento.objects.order_by("-id").values_list("id", flat=True)[300:])
    if sobra:
        Evento.objects.filter(id__in=sobra).delete()
