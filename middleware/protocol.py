from __future__ import annotations

import json
import time
import uuid

TIPOS = {
    "create_topic",
    "delete_topic",
    "publish",
    "subscribe",
    "unsubscribe",
    "ack",
    "error",
    "deliver",
    "ping",
    "pong",
    "stats",
    "broker_hello",
    "broker_topic",
    "broker_interest",
    "broker_publish",
}


def novo_id() -> str:
    return uuid.uuid4().hex


def mensagem(
    tipo: str,
    *,
    topic: str | None = None,
    payload: dict | None = None,
    id: str | None = None,
    client_id: str | None = None,
    origem: str | None = None,
    via: str | None = None,
) -> dict:
    msg = {"type": tipo, "id": id or novo_id(), "ts": time.time()}
    if topic is not None:
        msg["topic"] = topic
    if payload is not None:
        msg["payload"] = payload
    if client_id is not None:
        msg["client_id"] = client_id
    if origem is not None:
        msg["origem"] = origem
    if via is not None:
        msg["via"] = via
    return msg


def codificar(msg: dict) -> bytes:
    return json.dumps(msg, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def decodificar(dados: bytes) -> dict:
    msg = json.loads(dados.decode("utf-8"))
    if not isinstance(msg, dict) or msg.get("type") not in TIPOS or not msg.get("id"):
        raise ValueError("mensagem fora do protocolo")
    return msg


def decidir_publicacao(
    *,
    duplicada: bool,
    topico_existe: bool,
    tem_inscritos: bool,
    fila_topico_cheia: bool,
    fila_cliente_cheia: bool,
) -> str:
    if duplicada:
        return "duplicate"
    if not topico_existe:
        return "topico_inexistente"
    if not tem_inscritos:
        return "discarded_no_subscribers"
    if fila_topico_cheia or fila_cliente_cheia:
        return "fila_cheia"
    return "accepted"
