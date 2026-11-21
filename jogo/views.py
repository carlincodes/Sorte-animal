import secrets
from pathlib import Path

from django.conf import settings
from django.contrib import messages
from django.http import JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_POST

from jogo.animais import ANIMAIS
from jogo.models import AnimalExtra, Aposta, Jogada, Jogador, Opcao, Sala
from jogo.services import cliente, interpretar, publicar
from middleware.client import ErroMiddleware

EXTENSOES = {".png", ".jpg", ".jpeg", ".webp", ".gif"}


def _codigo_novo() -> str:
    alfabeto = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
    for _ in range(8):
        codigo = "".join(secrets.choice(alfabeto) for _ in range(5))
        if not Sala.objects.filter(codigo=codigo).exists():
            return codigo
    raise RuntimeError("não foi possível gerar um código")


def _sala(codigo: str) -> Sala:
    return get_object_or_404(Sala, codigo=codigo.upper())


def _anfitriao(request, sala: Sala) -> bool:
    return request.session.get("anfitriao_de") == sala.codigo


def _jogador(request, sala: Sala) -> Jogador | None:
    ident = request.session.get("jogador_id")
    if not ident:
        return None
    return Jogador.objects.filter(pk=ident, sala=sala).first()


def _limpar(texto: str, limite: int) -> str:
    return " ".join(texto.split())[:limite]


def inicio(request):
    if request.method == "POST":
        acao = request.POST.get("acao")
        if acao == "criar":
            nome = _limpar(request.POST.get("nome", ""), 40)
            titulo = _limpar(request.POST.get("titulo", ""), 60) or "Sorte Animal"
            if not nome:
                messages.error(request, "Diz como você quer aparecer para a turma.")
            else:
                sala = Sala.objects.create(codigo=_codigo_novo(), titulo=titulo, anfitriao=nome)
                request.session["anfitriao_de"] = sala.codigo
                return redirect("painel", codigo=sala.codigo)
        elif acao == "entrar":
            codigo = _limpar(request.POST.get("codigo", ""), 5).upper()
            if not Sala.objects.filter(codigo=codigo).exists():
                messages.error(request, "Não encontrei uma sala com esse código.")
            else:
                return redirect("sala", codigo=codigo)
    return render(request, "jogo/inicio.html")


def sala(request, codigo):
    jogo = _sala(codigo)
    jogador = _jogador(request, jogo)
    if request.method == "POST":
        nome = _limpar(request.POST.get("nome", ""), 40)
        if not nome:
            messages.error(request, "Escolhe um nome para entrar.")
        else:
            jogador = Jogador.objects.create(sala=jogo, nome=nome)
            request.session["jogador_id"] = jogador.id
            return redirect("sala", codigo=jogo.codigo)
    aberta = jogo.jogadas.filter(status="aberta").prefetch_related("opcoes").first()
    ultima = jogo.jogadas.filter(status="encerrada").order_by("-ordem").first()
    minha = None
    if jogador and aberta:
        minha = Aposta.objects.filter(jogada=aberta, jogador=jogador).select_related("opcao").first()
    return render(
        request,
        "jogo/sala.html",
        {
            "sala": jogo,
            "jogador": jogador,
            "anfitriao": _anfitriao(request, jogo),
            "aberta": aberta,
            "ultima": ultima,
            "minha": minha,
            "placar": jogo.jogadores.all()[:8],
        },
    )


def painel(request, codigo):
    jogo = _sala(codigo)
    if not _anfitriao(request, jogo):
        return render(request, "jogo/painel.html", {"sala": jogo, "permitido": False})
    if request.method == "POST":
        acao = request.POST.get("acao")
        if acao == "animal":
            _guardar_animal(request, jogo)
        elif acao == "jogada":
            _guardar_jogada(request, jogo)
        elif acao == "apagar":
            jogada = jogo.jogadas.filter(pk=request.POST.get("jogada"), status="rascunho").first()
            if jogada:
                jogada.delete()
        return redirect("painel", codigo=jogo.codigo)
    aberta = jogo.jogadas.filter(status="aberta").first()
    ultima = jogo.jogadas.filter(status="encerrada").order_by("-ordem").first()
    return render(
        request,
        "jogo/painel.html",
        {
            "sala": jogo,
            "permitido": True,
            "animais": ANIMAIS,
            "extras": jogo.extras.all(),
            "jogadas": jogo.jogadas.prefetch_related("opcoes"),
            "aberta": aberta,
            "ultima": ultima,
            "placar": jogo.jogadores.all()[:8],
        },
    )


def _guardar_animal(request, jogo: Sala) -> None:
    nome = _limpar(request.POST.get("nome", ""), 40)
    arquivo = request.FILES.get("imagem")
    if not nome or arquivo is None:
        messages.error(request, "O animal novo precisa de nome e de uma imagem.")
        return
    if jogo.extras.count() >= 24:
        messages.error(request, "Esta sala já tem animais novos demais.")
        return
    extensao = Path(arquivo.name).suffix.lower()
    if extensao not in EXTENSOES:
        messages.error(request, "Use uma imagem PNG, JPG, WEBP ou GIF.")
        return
    if arquivo.size > 2_000_000:
        messages.error(request, "A imagem precisa ter no máximo 2 MB.")
        return
    pasta = Path(settings.MEDIA_ROOT) / "animais" / jogo.codigo
    pasta.mkdir(parents=True, exist_ok=True)
    destino = pasta / f"{secrets.token_hex(8)}{extensao}"
    with destino.open("wb") as saida:
        for pedaco in arquivo.chunks():
            saida.write(pedaco)
    AnimalExtra.objects.create(
        sala=jogo,
        nome=nome,
        imagem=f"{settings.MEDIA_URL}animais/{jogo.codigo}/{destino.name}",
    )


def _guardar_jogada(request, jogo: Sala) -> None:
    grupos = []
    for valor in request.POST.getlist("grupo"):
        try:
            grupo = int(valor)
        except ValueError:
            continue
        if 1 <= grupo <= 25 and grupo not in grupos:
            grupos.append(grupo)
    extras = list(jogo.extras.filter(pk__in=request.POST.getlist("extra")))
    if len(grupos) + len(extras) < 2:
        messages.error(request, "Cada jogada precisa de pelo menos dois animais.")
        return
    ordem = (jogo.jogadas.order_by("-ordem").values_list("ordem", flat=True).first() or 0) + 1
    jogada = Jogada.objects.create(sala=jogo, ordem=ordem)
    for grupo in grupos:
        animal = ANIMAIS[grupo - 1]
        Opcao.objects.create(jogada=jogada, nome=animal["nome"], emoji=animal["emoji"], grupo=grupo)
    for extra in extras:
        Opcao.objects.create(jogada=jogada, nome=extra.nome, imagem=extra.imagem)


def _publicar_ou_erro(topic: str, payload: dict):
    try:
        resposta = publicar(topic, payload)
    except ErroMiddleware as exc:
        return None, JsonResponse({"ok": False, "erro": str(exc)}, status=503)
    ok, texto = interpretar(resposta)
    if not ok:
        return None, JsonResponse({"ok": False, "erro": texto, "protocolo": resposta}, status=409)
    return resposta, None


@require_POST
def api_abrir(request, codigo):
    jogo = _sala(codigo)
    if not _anfitriao(request, jogo):
        return JsonResponse({"ok": False, "erro": "Só quem criou a sala abre a jogada."}, status=403)
    if jogo.jogadas.filter(status="aberta").exists():
        return JsonResponse({"ok": False, "erro": "Já existe uma jogada aberta."}, status=409)
    jogada = jogo.jogadas.filter(pk=request.POST.get("jogada") or _corpo_id(request), status="rascunho").first()
    if jogada is None:
        return JsonResponse({"ok": False, "erro": "Essa jogada não está pronta para abrir."}, status=404)
    resposta, erro = _publicar_ou_erro(
        "jogo/rodada",
        {"evento": "aberta", "sala": jogo.codigo, "jogada_id": jogada.id, "ordem": jogada.ordem},
    )
    if erro:
        return erro
    return JsonResponse({"ok": True, "mensagem": f"Jogada {jogada.ordem} publicada.", "protocolo": resposta})


@require_POST
def api_sortear(request, codigo):
    jogo = _sala(codigo)
    if not _anfitriao(request, jogo):
        return JsonResponse({"ok": False, "erro": "Só quem criou a sala sorteia."}, status=403)
    jogada = jogo.jogadas.filter(status="aberta").prefetch_related("opcoes").first()
    if jogada is None:
        return JsonResponse({"ok": False, "erro": "Não há jogada aberta."}, status=409)
    opcoes = list(jogada.opcoes.all())
    if not opcoes:
        return JsonResponse({"ok": False, "erro": "Essa jogada não tem animais."}, status=409)
    opcao = secrets.choice(opcoes)
    resposta, erro = _publicar_ou_erro(
        "jogo/resultado",
        {
            "evento": "divulgar",
            "sala": jogo.codigo,
            "jogada_id": jogada.id,
            "opcao_id": opcao.id,
            "animal": opcao.nome,
        },
    )
    if erro:
        return erro
    return JsonResponse({"ok": True, "mensagem": f"Sorteado: {opcao.nome}.", "protocolo": resposta})


@require_POST
def api_apostar(request, codigo):
    jogo = _sala(codigo)
    jogador = _jogador(request, jogo)
    if jogador is None:
        return JsonResponse({"ok": False, "erro": "Entre na sala antes de escolher."}, status=403)
    jogada = jogo.jogadas.filter(status="aberta").first()
    if jogada is None:
        return JsonResponse({"ok": False, "erro": "A jogada ainda não está aberta."}, status=409)
    if Aposta.objects.filter(jogada=jogada, jogador=jogador).exists():
        return JsonResponse({"ok": False, "erro": "Você já escolheu nesta jogada."}, status=409)
    opcao = jogada.opcoes.filter(pk=_corpo_id(request)).first()
    if opcao is None:
        return JsonResponse({"ok": False, "erro": "Esse animal não está nesta jogada."}, status=400)
    resposta, erro = _publicar_ou_erro(
        "jogo/palpites",
        {
            "sala": jogo.codigo,
            "jogada_id": jogada.id,
            "jogador_id": jogador.id,
            "opcao_id": opcao.id,
            "nome": jogador.nome,
            "animal": opcao.nome,
        },
    )
    if erro:
        return erro
    return JsonResponse({"ok": True, "mensagem": f"Você escolheu {opcao.nome}.", "protocolo": resposta})


def api_estado(request, codigo):
    jogo = _sala(codigo)
    anfitriao = _anfitriao(request, jogo)
    jogador = _jogador(request, jogo)
    aberta = jogo.jogadas.filter(status="aberta").first()
    ultima = jogo.jogadas.filter(status="encerrada").order_by("-ordem").first()
    foco = aberta or ultima
    apostas = []
    if foco and (anfitriao or foco.status == "encerrada"):
        for aposta in foco.apostas.select_related("jogador", "opcao"):
            apostas.append(
                {
                    "nome": aposta.jogador.nome,
                    "animal": aposta.opcao.nome,
                    "acertou": aposta.acertou,
                    "pontos": aposta.pontos,
                }
            )
    return JsonResponse(
        {
            "codigo": jogo.codigo,
            "jogada_id": aberta.id if aberta else (ultima.id if ultima else None),
            "status": aberta.status if aberta else (ultima.status if ultima else "espera"),
            "animal": ultima.animal_sorteado if ultima and not aberta else "",
            "apostas": apostas,
            "presentes": jogo.jogadores.count(),
            "placar": [{"nome": item.nome, "pontos": item.pontos} for item in jogo.jogadores.all()[:8]],
            "conectado": bool(cliente and cliente.conectado),
            "broker": cliente.broker_ativo if cliente else "",
            "eu": jogador.nome if jogador else "",
        }
    )


def _corpo_id(request) -> str:
    import json

    if request.POST.get("jogada") or request.POST.get("opcao"):
        return request.POST.get("jogada") or request.POST.get("opcao") or ""
    try:
        corpo = json.loads(request.body or "{}")
    except json.JSONDecodeError:
        return ""
    return str(corpo.get("jogada") or corpo.get("opcao") or "")
