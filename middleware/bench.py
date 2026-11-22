"""Mede latência e vazão da confirmação de publicação.

Uso, na pasta do projeto:
    python -m middleware.bench --n 200
"""

from __future__ import annotations

import argparse
import subprocess
import sys
import time
from pathlib import Path

from middleware.client import ClienteMiddleware

ROOT = Path(__file__).resolve().parent.parent


def percentil(valores: list[float], p: float) -> float:
    if not valores:
        return 0
    ordenados = sorted(valores)
    indice = min(len(ordenados) - 1, max(0, int(round((p / 100) * (len(ordenados) - 1)))))
    return ordenados[indice]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--n", type=int, default=200)
    args = parser.parse_args()
    logs = []
    processos = []
    try:
        for nome, porta, par in (
            ("bench-a", "9201", "127.0.0.1:9202"),
            ("bench-b", "9202", "127.0.0.1:9201"),
        ):
            log = open(ROOT / f".{nome}.log", "w", encoding="utf-8")
            logs.append(log)
            processos.append(subprocess.Popen(
                [sys.executable, "-m", "middleware.broker", "--nome", nome, "--porta", porta, "--par", par],
                cwd=ROOT,
                stdout=log,
                stderr=subprocess.STDOUT,
            ))
        time.sleep(0.6)
        brokers = [("127.0.0.1", 9201), ("127.0.0.1", 9202)]
        recebidas = []
        assinante = ClienteMiddleware(brokers, client_id="bench-sub", timeout=1)
        publicador = ClienteMiddleware(brokers, client_id="bench-pub", timeout=1, indice_inicial=1)
        assinante.iniciar()
        publicador.iniciar()
        assinante.criar_topico("lab/bench")
        assinante.inscrever("lab/bench", lambda msg: recebidas.append(msg))
        time.sleep(0.4)
        latencias = []
        inicio = time.perf_counter()
        for i in range(args.n):
            t0 = time.perf_counter()
            publicador.publicar("lab/bench", {"n": i})
            latencias.append((time.perf_counter() - t0) * 1000)
        gasto = time.perf_counter() - inicio
        time.sleep(0.5)
        print(f"Mensagens publicadas: {args.n}")
        print(f"Entregas recebidas:   {len(recebidas)}")
        print(f"Latência p50:         {percentil(latencias, 50):.2f} ms")
        print(f"Latência p95:         {percentil(latencias, 95):.2f} ms")
        print(f"Vazão:                {args.n / gasto:.0f} publicações/s")
        assinante.encerrar()
        publicador.encerrar()
    finally:
        for processo in processos:
            if processo.poll() is None:
                processo.terminate()
        for log in logs:
            log.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
