from django.db import models


class Participante(models.Model):
    nome = models.CharField(max_length=40)
    pontos = models.IntegerField(default=0)
    criado_em = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-pontos", "nome"]

    def __str__(self):
        return self.nome


class Rodada(models.Model):
    numero = models.PositiveIntegerField()
    status = models.CharField(max_length=12, default="aberta")
    numero_sorteado = models.CharField(max_length=2, blank=True)
    grupo_sorteado = models.PositiveSmallIntegerField(null=True, blank=True)
    animal_sorteado = models.CharField(max_length=30, blank=True)
    aberta_em = models.DateTimeField(auto_now_add=True)
    encerrada_em = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-id"]

    def __str__(self):
        return f"Rodada {self.numero}"


class Palpite(models.Model):
    participante = models.ForeignKey(Participante, related_name="palpites", on_delete=models.CASCADE)
    rodada = models.ForeignKey(Rodada, related_name="palpites", on_delete=models.CASCADE)
    grupo = models.PositiveSmallIntegerField()
    animal = models.CharField(max_length=30)
    numero = models.CharField(max_length=2, blank=True)
    pontos = models.IntegerField(default=0)
    acertou_grupo = models.BooleanField(default=False)
    acertou_numero = models.BooleanField(default=False)
    mensagem_id = models.CharField(max_length=64, unique=True)
    criado_em = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["participante", "rodada"], name="um_palpite_por_rodada"),
        ]
        ordering = ["id"]


class Evento(models.Model):
    topico = models.CharField(max_length=80)
    resumo = models.CharField(max_length=240)
    bruto = models.JSONField(default=dict)
    criado_em = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-id"]


class Sala(models.Model):
    codigo = models.CharField(max_length=5, unique=True)
    titulo = models.CharField(max_length=60)
    anfitriao = models.CharField(max_length=40)
    criada_em = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"{self.codigo} · {self.titulo}"


class AnimalExtra(models.Model):
    sala = models.ForeignKey(Sala, related_name="extras", on_delete=models.CASCADE)
    nome = models.CharField(max_length=40)
    imagem = models.CharField(max_length=240)
    criado_em = models.DateTimeField(auto_now_add=True)


class Jogada(models.Model):
    sala = models.ForeignKey(Sala, related_name="jogadas", on_delete=models.CASCADE)
    ordem = models.PositiveIntegerField()
    status = models.CharField(max_length=12, default="rascunho")
    animal_sorteado = models.CharField(max_length=40, blank=True)
    emoji_sorteado = models.CharField(max_length=8, blank=True)
    imagem_sorteada = models.CharField(max_length=240, blank=True)
    aberta_em = models.DateTimeField(null=True, blank=True)
    encerrada_em = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["ordem"]
        constraints = [
            models.UniqueConstraint(fields=["sala", "ordem"], name="ordem_unica_na_sala"),
        ]


class Opcao(models.Model):
    jogada = models.ForeignKey(Jogada, related_name="opcoes", on_delete=models.CASCADE)
    nome = models.CharField(max_length=40)
    emoji = models.CharField(max_length=8, blank=True)
    grupo = models.PositiveSmallIntegerField(null=True, blank=True)
    imagem = models.CharField(max_length=240, blank=True)

    class Meta:
        ordering = ["id"]


class Jogador(models.Model):
    sala = models.ForeignKey(Sala, related_name="jogadores", on_delete=models.CASCADE)
    nome = models.CharField(max_length=40)
    pontos = models.IntegerField(default=0)
    criado_em = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-pontos", "nome"]


class Aposta(models.Model):
    jogada = models.ForeignKey(Jogada, related_name="apostas", on_delete=models.CASCADE)
    jogador = models.ForeignKey(Jogador, related_name="apostas", on_delete=models.CASCADE)
    opcao = models.ForeignKey(Opcao, related_name="apostas", on_delete=models.CASCADE)
    mensagem_id = models.CharField(max_length=64, unique=True)
    acertou = models.BooleanField(default=False)
    pontos = models.IntegerField(default=0)
    criado_em = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["jogador", "jogada"], name="uma_aposta_por_jogada"),
        ]
