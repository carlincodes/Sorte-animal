"""Confere as regras do middleware sem abrir o navegador.

Uso, na pasta do projeto:
    python -m middleware.verificar
"""

from __future__ import annotations

import os
import socket
import subprocess
import sys
import time
from pathlib import Path

from middleware.client import ClienteMiddleware
from middleware.protocol import codificar, decodificar, decidir_publicacao, mensagem

ROOT = Path(__file__).resolve().parent.parent
LOGS = ROOT / ".verificar-logs"
FALHAS = []


def checar(nome: str, condicao: bool, detalhe: str = "") -> None:
    marca = "ok" if condicao else "FALHOU"
    print(f"[{marca}] {nome}" + (f" — {detalhe}" if detalhe and not condicao else ""))
    if not condicao:
        FALHAS.append(nome)


def subir(nome: str, porta: int, par: str, extra: list[str] | None = None):
    LOGS.mkdir(exist_ok=True)
    comando = [
        sys.executable, "-m", "middleware.broker",
        "--nome", nome, "--porta", str(porta), "--par", par,
    ]
    if extra:
        comando.extend(extra)
    log = open(LOGS / f"{nome}.log", "w", encoding="utf-8")
    ambiente = os.environ.copy()
    ambiente["PYTHONUNBUFFERED"] = "1"
    processo = subprocess.Popen(
        comando,
        cwd=ROOT,
        stdout=log,
        stderr=subprocess.STDOUT,
        env=ambiente,
    )
    return processo, log


def rpc(porta: int, msg: dict, timeout: float = 1.0) -> dict:
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.settimeout(timeout)
    sock.bind(("127.0.0.1", 0))
    try:
        sock.sendto(codificar(msg), ("127.0.0.1", porta))
        dados, _ = sock.recvfrom(65535)
        return decodificar(dados)
    finally:
        sock.close()


def esperar(porta: int) -> None:
    for _ in range(40):
        try:
            resp = rpc(porta, mensagem("ping"), timeout=0.3)
            if resp.get("type") == "pong":
                return
        except (TimeoutError, OSError, ValueError):
            time.sleep(0.1)
    raise RuntimeError(f"broker na porta {porta} não respondeu")


def estado(resp: dict) -> str:
    payload = resp.get("payload") or {}
    return payload.get("status") or payload.get("codigo") or resp.get("type")


def regras() -> None:
    checar("sem inscritos descarta", decidir_publicacao(
        duplicada=False, topico_existe=True, tem_inscritos=False,
        fila_topico_cheia=False, fila_cliente_cheia=False,
    ) == "discarded_no_subscribers")
    checar("tópico ausente", decidir_publicacao(
        duplicada=False, topico_existe=False, tem_inscritos=False,
        fila_topico_cheia=False, fila_cliente_cheia=False,
    ) == "topico_inexistente")
    checar("fila cheia rejeita", decidir_publicacao(
        duplicada=False, topico_existe=True, tem_inscritos=True,
        fila_topico_cheia=False, fila_cliente_cheia=True,
    ) == "fila_cheia")
    checar("duplicata não segue", decidir_publicacao(
        duplicada=True, topico_existe=True, tem_inscritos=True,
        fila_topico_cheia=False, fila_cliente_cheia=False,
    ) == "duplicate")
    checar("com inscrito aceita", decidir_publicacao(
        duplicada=False, topico_existe=True, tem_inscritos=True,
        fila_topico_cheia=False, fila_cliente_cheia=False,
    ) == "accepted")


def esperar_entrega(caixa: list, quantidade: int, segundos: float = 3) -> bool:
    limite = time.time() + segundos
    while time.time() < limite:
        if len(caixa) >= quantidade:
            return True
        time.sleep(0.05)
    return False


def main() -> int:
    regras()
    processos = []
    try:
        par_a = subir("broker-a", 9101, "127.0.0.1:9102")
        par_b = subir("broker-b", 9102, "127.0.0.1:9101")
        processos.extend([par_a, par_b])
        esperar(9101)
        esperar(9102)

        brokers = [("127.0.0.1", 9101), ("127.0.0.1", 9102)]
        assinante = ClienteMiddleware(brokers, client_id="assinante", timeout=0.6, intervalo_ping=0.3)
        publicador = ClienteMiddleware(
            brokers, client_id="publicador", timeout=0.6, indice_inicial=1, intervalo_ping=0.3,
        )
        assinante.iniciar()
        publicador.iniciar()
        caixa: list[dict] = []

        criado = assinante.criar_topico("lab/vazio")
        checar("cria tópico", estado(criado) in {"created", "exists"}, estado(criado))
        vazio = assinante.publicar("lab/vazio", {"x": 1})
        checar("publicação sem inscrito é descartada", estado(vazio) == "discarded_no_subscribers", estado(vazio))

        def guardar(msg):
            caixa.append(msg)

        assinante.criar_topico("lab/cruzado")
        publicador.criar_topico("lab/cruzado")
        assinante.inscrever("lab/cruzado", guardar)
        recebeu = False
        resposta = {}
        for _ in range(20):
            resposta = publicador.publicar("lab/cruzado", {"texto": "pelo outro broker"})
            if caixa or estado(resposta) == "accepted":
                recebeu = esperar_entrega(caixa, 1)
                if recebeu:
                    break
            time.sleep(0.2)
        checar("broker B entrega no assinante do broker A", recebeu, estado(resposta))
        checar("uma única entrega", len(caixa) == 1, str(len(caixa)))

        antes = len(caixa)
        duplicada = mensagem(
            "publish", topic="lab/cruzado", id="id-fixo-verificar",
            payload={"n": 1}, client_id="manual",
        )
        primeira = rpc(9101, duplicada)
        segunda = rpc(9101, duplicada)
        time.sleep(0.4)
        checar("primeira cópia aceita", estado(primeira) == "accepted", estado(primeira))
        checar("segunda cópia reconhecida", estado(segunda) == "duplicate", estado(segunda))
        checar("duplicata não foi entregue de novo", len(caixa) == antes + 1, str(len(caixa) - antes))

        assinante.excluir_topico("lab/cruzado")
        time.sleep(0.3)
        apagado = assinante.publicar("lab/cruzado", {"x": 1})
        checar("tópico excluído rejeita publicação", estado(apagado) == "topico_inexistente", estado(apagado))

        lento_a = subir("broker-lento", 9103, "127.0.0.1:9104", ["--fila-cliente", "1", "--atraso-ms", "400"])
        lento_b = subir("broker-par-lento", 9104, "127.0.0.1:9103")
        processos.extend([lento_a, lento_b])
        esperar(9103)
        fila_brokers = [("127.0.0.1", 9103), ("127.0.0.1", 9104)]
        ouvinte = ClienteMiddleware(fila_brokers, client_id="ouvinte", timeout=0.6, intervalo_ping=1)
        rapido = ClienteMiddleware(fila_brokers, client_id="rapido", timeout=0.6, intervalo_ping=1)
        ouvinte.iniciar()
        rapido.iniciar()
        ouvinte.criar_topico("lab/rajada")
        ouvinte.inscrever("lab/rajada", lambda msg: None)
        time.sleep(0.2)
        respostas = [rapido.publicar("lab/rajada", {"n": i}) for i in range(6)]
        codigos = [estado(item) for item in respostas]
        checar("rajada aceita a primeira", codigos[0] == "accepted", " ".join(codigos))
        checar("rajada rejeita quando a fila enche", "fila_cheia" in codigos, " ".join(codigos))

        assinante.criar_topico("lab/vivo")
        caixa_viva: list[dict] = []
        assinante.inscrever("lab/vivo", lambda msg: caixa_viva.append(msg))
        time.sleep(0.4)
        par_a[0].terminate()
        mudou = False
        limite = time.time() + 6
        while time.time() < limite:
            if assinante.broker_ativo.endswith(":9102"):
                mudou = True
                break
            time.sleep(0.2)
        checar("cliente troca para o broker vivo", mudou, assinante.broker_ativo)
        resposta_viva = publicador.publicar("lab/vivo", {"vivo": True})
        checar(
            "publicação funciona depois da queda",
            estado(resposta_viva) == "accepted" and esperar_entrega(caixa_viva, 1),
            estado(resposta_viva),
        )
        for cliente in (assinante, publicador, ouvinte, rapido):
            cliente.encerrar()
    finally:
        for processo, log in processos:
            if processo.poll() is None:
                processo.terminate()
            try:
                processo.wait(timeout=3)
            except subprocess.TimeoutExpired:
                processo.kill()
            log.close()
    if FALHAS:
        print("\nFalharam:", ", ".join(FALHAS))
        for caminho in sorted(LOGS.glob("*.log")):
            texto = caminho.read_text(encoding="utf-8", errors="replace")
            print(f"\n----- {caminho.name} -----\n{texto[-2500:]}")
        return 1
    print("\nMiddleware conferido.")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"erro inesperado: {exc}")
        raise
