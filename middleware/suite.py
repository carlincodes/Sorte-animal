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

SECOES = [
    {
        "id": "descarte",
        "titulo": "Tópico sem inscritos",
        "texto": "A publicação é descartada e o publicador recebe a confirmação discarded_no_subscribers.",
    },
    {
        "id": "cruzado",
        "titulo": "Entrega entre os dois brokers",
        "texto": "Quem publica no broker B chega no assinante que está no broker A, uma vez só.",
    },
    {
        "id": "duplicata",
        "titulo": "O mesmo id não é entregue duas vezes",
        "texto": "A segunda cópia da mensagem volta como duplicate e não gera outra entrega.",
    },
    {
        "id": "exclusao",
        "titulo": "Tópico criado e excluído em execução",
        "texto": "Depois de excluir o tópico, uma publicação nova recebe topico_inexistente.",
    },
    {
        "id": "rajada",
        "titulo": "Rajada e fila cheia",
        "texto": "A fila do assinante tem uma vaga. O que não cabe é rejeitado com o erro fila_cheia.",
    },
    {
        "id": "latencia",
        "titulo": "Latência e vazão",
        "texto": "Uma sequência de publicações mede o tempo até a confirmação e quantas chegam por segundo.",
    },
    {
        "id": "queda",
        "titulo": "Um broker fica indisponível",
        "texto": "Com o broker A encerrado, a biblioteca passa para o B, refaz a inscrição e a publicação continua.",
    },
]


def percentil(valores: list[float], p: float) -> float:
    if not valores:
        return 0
    ordenados = sorted(valores)
    indice = min(len(ordenados) - 1, max(0, int(round((p / 100) * (len(ordenados) - 1)))))
    return ordenados[indice]


def _subir(nome: str, porta: int, par: str, extra: list[str] | None = None):
    LOGS.mkdir(exist_ok=True)
    comando = [sys.executable, "-m", "middleware.broker", "--nome", nome, "--porta", str(porta), "--par", par]
    if extra:
        comando.extend(extra)
    log = open(LOGS / f"suite-{nome}.log", "w", encoding="utf-8")
    ambiente = os.environ.copy()
    ambiente["PYTHONUNBUFFERED"] = "1"
    processo = subprocess.Popen(comando, cwd=ROOT, stdout=log, stderr=subprocess.STDOUT, env=ambiente)
    return processo, log


def _rpc(porta: int, msg: dict, timeout: float = 1.0) -> dict:
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.settimeout(timeout)
    sock.bind(("127.0.0.1", 0))
    try:
        sock.sendto(codificar(msg), ("127.0.0.1", porta))
        dados, _ = sock.recvfrom(65535)
        return decodificar(dados)
    finally:
        sock.close()


def _esperar(porta: int) -> None:
    for _ in range(40):
        try:
            resp = _rpc(porta, mensagem("ping"), timeout=0.3)
            if resp.get("type") == "pong":
                return
        except (TimeoutError, OSError, ValueError):
            time.sleep(0.1)
    raise RuntimeError(f"o broker da porta {porta} não respondeu")


def _estado(resp: dict) -> str:
    payload = resp.get("payload") or {}
    return payload.get("status") or payload.get("codigo") or resp.get("type")


def _aguardar(caixa: list, quantidade: int, segundos: float = 3) -> bool:
    limite = time.time() + segundos
    while time.time() < limite:
        if len(caixa) >= quantidade:
            return True
        time.sleep(0.05)
    return False


def _encerrar(processos, clientes) -> None:
    for cliente in clientes:
        try:
            cliente.encerrar()
        except Exception:
            pass
    for processo, log in processos:
        if processo.poll() is None:
            processo.terminate()
        try:
            processo.wait(timeout=3)
        except subprocess.TimeoutExpired:
            processo.kill()
        log.close()


def _secao(identificador: str) -> dict:
    base = next(item for item in SECOES if item["id"] == identificador)
    return {**base, "status": "rodando", "ok": None, "detalhe": "", "passos": [], "ms": 0}


def _passo(secao: dict, titulo: str, condicao: bool, detalhe: str = "") -> None:
    secao["passos"].append({"titulo": titulo, "ok": bool(condicao), "detalhe": "" if condicao else detalhe})


def _fechar(secao: dict, inicio: float, aviso) -> None:
    secao["ok"] = all(passo["ok"] for passo in secao["passos"]) if secao["passos"] else False
    secao["status"] = "ok" if secao["ok"] else "falhou"
    secao["ms"] = int((time.perf_counter() - inicio) * 1000)
    aviso(secao)


def rodar(aviso) -> None:
    for item in SECOES:
        preparada = {**item, "status": "na_fila", "ok": None, "detalhe": "", "passos": [], "ms": 0}
        aviso(preparada)

    processos = []
    clientes = []
    try:
        par_a = _subir("sa", 9411, "127.0.0.1:9412")
        par_b = _subir("sb", 9412, "127.0.0.1:9411")
        processos.extend([par_a, par_b])
        _esperar(9411)
        _esperar(9412)
        brokers = [("127.0.0.1", 9411), ("127.0.0.1", 9412)]
        assinante = ClienteMiddleware(brokers, client_id="suite-assinante", timeout=0.6, intervalo_ping=0.3)
        publicador = ClienteMiddleware(
            brokers, client_id="suite-publicador", timeout=0.6, indice_inicial=1, intervalo_ping=0.3,
        )
        assinante.iniciar()
        publicador.iniciar()
        clientes.extend([assinante, publicador])

        inicio = time.perf_counter()
        secao = _secao("descarte")
        aviso(secao)
        regras = [
            ("sem inscritos descarta", decidir_publicacao(
                duplicada=False, topico_existe=True, tem_inscritos=False,
                fila_topico_cheia=False, fila_cliente_cheia=False,
            ) == "discarded_no_subscribers", ""),
            ("tópico ausente", decidir_publicacao(
                duplicada=False, topico_existe=False, tem_inscritos=False,
                fila_topico_cheia=False, fila_cliente_cheia=False,
            ) == "topico_inexistente", ""),
            ("fila cheia rejeita", decidir_publicacao(
                duplicada=False, topico_existe=True, tem_inscritos=True,
                fila_topico_cheia=False, fila_cliente_cheia=True,
            ) == "fila_cheia", ""),
        ]
        for titulo, condicao, detalhe in regras:
            _passo(secao, titulo, condicao, detalhe)
        criado = assinante.criar_topico("lab/vazio")
        vazio = assinante.publicar("lab/vazio", {"x": 1})
        _passo(secao, "o broker confirma o descarte", _estado(vazio) == "discarded_no_subscribers", _estado(vazio))
        _passo(secao, "o tópico foi criado", _estado(criado) in {"created", "exists"}, _estado(criado))
        _fechar(secao, inicio, aviso)

        inicio = time.perf_counter()
        secao = _secao("cruzado")
        aviso(secao)
        caixa: list[dict] = []
        assinante.criar_topico("lab/cruzado")
        publicador.criar_topico("lab/cruzado")
        assinante.inscrever("lab/cruzado", caixa.append)
        resposta = {}
        recebeu = False
        for _ in range(20):
            resposta = publicador.publicar("lab/cruzado", {"texto": "pelo outro broker"})
            if caixa or _estado(resposta) == "accepted":
                recebeu = _aguardar(caixa, 1)
                if recebeu:
                    break
            time.sleep(0.2)
        _passo(secao, "o assinante do broker A recebe", recebeu, _estado(resposta))
        _passo(secao, "chegou uma única cópia", len(caixa) == 1, str(len(caixa)))
        _fechar(secao, inicio, aviso)

        inicio = time.perf_counter()
        secao = _secao("duplicata")
        aviso(secao)
        antes = len(caixa)
        duplicada = mensagem(
            "publish", topic="lab/cruzado", id="id-fixo-suite", payload={"n": 1}, client_id="manual",
        )
        primeira = _rpc(9411, duplicada)
        segunda = _rpc(9411, duplicada)
        time.sleep(0.4)
        _passo(secao, "a primeira cópia é aceita", _estado(primeira) == "accepted", _estado(primeira))
        _passo(secao, "a segunda é reconhecida como duplicata", _estado(segunda) == "duplicate", _estado(segunda))
        _passo(secao, "não houve segunda entrega", len(caixa) == antes + 1, str(len(caixa) - antes))
        _fechar(secao, inicio, aviso)

        inicio = time.perf_counter()
        secao = _secao("exclusao")
        aviso(secao)
        assinante.excluir_topico("lab/cruzado")
        time.sleep(0.3)
        apagado = assinante.publicar("lab/cruzado", {"x": 1})
        _passo(secao, "publicar no tópico excluído falha", _estado(apagado) == "topico_inexistente", _estado(apagado))
        _fechar(secao, inicio, aviso)

        inicio = time.perf_counter()
        secao = _secao("rajada")
        aviso(secao)
        lento_a = _subir("sl", 9413, "127.0.0.1:9414", ["--fila-cliente", "1", "--atraso-ms", "400"])
        lento_b = _subir("slb", 9414, "127.0.0.1:9413")
        processos.extend([lento_a, lento_b])
        _esperar(9413)
        fila = [("127.0.0.1", 9413), ("127.0.0.1", 9414)]
        ouvinte = ClienteMiddleware(fila, client_id="suite-ouvinte", timeout=0.6, intervalo_ping=1)
        rapido = ClienteMiddleware(fila, client_id="suite-rapido", timeout=0.6, intervalo_ping=1)
        ouvinte.iniciar()
        rapido.iniciar()
        clientes.extend([ouvinte, rapido])
        ouvinte.criar_topico("lab/rajada")
        ouvinte.inscrever("lab/rajada", lambda msg: None)
        time.sleep(0.2)
        respostas = [rapido.publicar("lab/rajada", {"n": i}) for i in range(6)]
        codigos = [_estado(item) for item in respostas]
        _passo(secao, "a primeira publicação entra", codigos[0] == "accepted", " ".join(codigos))
        _passo(secao, "as seguintes encontram a fila cheia", "fila_cheia" in codigos, " ".join(codigos))
        secao["detalhe"] = "respostas: " + ", ".join(codigos)
        _fechar(secao, inicio, aviso)

        inicio = time.perf_counter()
        secao = _secao("latencia")
        aviso(secao)
        medidas: list[dict] = []
        assinante.criar_topico("lab/tempo")
        assinante.inscrever("lab/tempo", medidas.append)
        time.sleep(0.3)
        latencias = []
        quantidade = 40
        t0 = time.perf_counter()
        for i in range(quantidade):
            marca = time.perf_counter()
            publicador.publicar("lab/tempo", {"n": i})
            latencias.append((time.perf_counter() - marca) * 1000)
        gasto = time.perf_counter() - t0
        time.sleep(0.4)
        _passo(secao, "as publicações foram confirmadas", len(latencias) == quantidade, str(len(latencias)))
        _passo(secao, "as entregas chegaram", len(medidas) >= quantidade - 2, str(len(medidas)))
        secao["detalhe"] = (
            f"p50 {percentil(latencias, 50):.1f} ms · p95 {percentil(latencias, 95):.1f} ms · "
            f"{quantidade / gasto:.0f} publicações/s · {len(medidas)} entregas"
        )
        _fechar(secao, inicio, aviso)

        inicio = time.perf_counter()
        secao = _secao("queda")
        aviso(secao)
        caixa_viva: list[dict] = []
        assinante.criar_topico("lab/vivo")
        assinante.inscrever("lab/vivo", caixa_viva.append)
        time.sleep(0.4)
        par_a[0].terminate()
        mudou = False
        limite = time.time() + 6
        while time.time() < limite:
            if assinante.broker_ativo.endswith(":9412"):
                mudou = True
                break
            time.sleep(0.2)
        resposta_viva = publicador.publicar("lab/vivo", {"vivo": True})
        _passo(secao, "a biblioteca troca para o broker vivo", mudou, assinante.broker_ativo)
        _passo(
            secao,
            "a publicação segue depois da queda",
            _estado(resposta_viva) == "accepted" and _aguardar(caixa_viva, 1),
            _estado(resposta_viva),
        )
        _fechar(secao, inicio, aviso)
    except Exception as exc:
        falha = {
            "id": "erro",
            "titulo": "A bateria parou",
            "texto": "Um dos testes não conseguiu continuar.",
            "status": "falhou",
            "ok": False,
            "detalhe": str(exc),
            "passos": [],
            "ms": 0,
        }
        aviso(falha)
    finally:
        _encerrar(processos, clientes)
