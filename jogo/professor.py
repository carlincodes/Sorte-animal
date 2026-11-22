import threading

from django.http import JsonResponse
from django.shortcuts import render
from django.views.decorators.http import require_GET, require_POST

from middleware.suite import SECOES, rodar

_trava = threading.Lock()
_painel = {"rodando": False, "atual": "", "secoes": []}


def _novas():
    return [
        {**item, "status": "pronto", "ok": None, "detalhe": "", "passos": [], "ms": 0}
        for item in SECOES
    ]


def pagina(request):
    return render(request, "jogo/professor.html")


@require_GET
def estado(request):
    with _trava:
        if not _painel["secoes"]:
            _painel["secoes"] = _novas()
        return JsonResponse(
            {"rodando": _painel["rodando"], "atual": _painel["atual"], "secoes": _painel["secoes"]}
        )


@require_POST
def rodar_testes(request):
    with _trava:
        if _painel["rodando"]:
            return JsonResponse({"ok": False, "erro": "Os testes já estão em andamento."}, status=409)
        _painel["rodando"] = True
        _painel["atual"] = "Preparando os brokers de teste"
        _painel["secoes"] = _novas()

    def trabalhar():
        def aviso(secao):
            with _trava:
                _painel["atual"] = secao["titulo"]
                for indice, item in enumerate(_painel["secoes"]):
                    if item["id"] == secao["id"]:
                        _painel["secoes"][indice] = secao
                        return
                _painel["secoes"].append(secao)

        try:
            rodar(aviso)
        finally:
            with _trava:
                _painel["rodando"] = False
                _painel["atual"] = ""

    threading.Thread(target=trabalhar, name="testes-professor", daemon=True).start()
    return JsonResponse({"ok": True, "mensagem": "Testes iniciados."})
