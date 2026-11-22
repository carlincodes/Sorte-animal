# Sorte Animal

Sorte Animal é um jogo ao vivo em que uma pessoa cria a sala, monta várias jogadas e escolhe quais animais aparecem em cada uma. Pode usar os 25 animais da plataforma ou incluir outro, com nome e foto. O código de cinco caracteres é compartilhado com a turma. Quem entra com esse código vê só os animais da jogada aberta, escolhe um e espera o sorteio. Acertar vale 10 pontos.

O jogo demonstra um middleware publish/subscribe. Dois brokers ativos trocam mensagens em UDP e JSON. A biblioteca cliente publica, inscreve e, se um broker cair, passa para o outro. O middleware não conhece a regra do jogo: o Django abre a jogada, grava o palpite e calcula o placar quando a mensagem volta.

Trabalho de Programação Distribuída (IFPB), equipe Carlos Silvio e Matheus Barbosa.

## Páginas

Com a aplicação no ar em http://127.0.0.1:8000/:

- **Início:** http://127.0.0.1:8000/ — criar uma sala ou entrar com o código.
- **Painel do jogo:** depois de criar a sala, quem criou cai em http://127.0.0.1:8000/s/CODIGO/painel/. Ali se montam as jogadas, se abre a rodada e se faz o sorteio. `CODIGO` é o identificador de cinco caracteres mostrado na tela.
- **Sala de quem joga:** http://127.0.0.1:8000/s/CODIGO/ — a pessoa entra com o nome e escolhe o animal.
- **Painel de testes:** http://127.0.0.1:8000/testes/ — também pelo link **Painel de testes** no topo. O botão **Aplicar os testes** executa as regras do middleware (descarte, entrega entre brokers, duplicata, exclusão de tópico, fila cheia, latência e queda de um broker) sem usar as portas do jogo.

## Subir na apresentação

Na pasta do projeto, no PowerShell:

```powershell
.\iniciar.ps1
```

Isso instala o Django se precisar, sobe os brokers nas portas 9001 e 9002 e abre o site em http://127.0.0.1:8000/. Para encerrar, feche as janelas dos brokers e use Ctrl+C no site, ou rode `.\parar.ps1`.

## O que mostrar

1. Na página inicial, crie uma sala. O código grande é o que a turma usa para entrar. Dá para copiar o link.
2. Monte uma jogada marcando animais da plataforma, ou adicione um animal com nome e foto.
3. Abra a jogada. Em outra janela, entre com o código, escolha um animal e confirme.
4. No painel de quem criou, a escolha aparece quando o broker entrega. Sorteie. Quem acerta ganha 10 pontos.
5. Abra **Painel de testes** e clique em **Aplicar os testes**. Os cartões mostram descarte, entrega entre brokers, duplicata, exclusão de tópico, fila cheia, latência e a queda de um broker.

O botão **Aplicar os testes**, no Painel de testes, aplica essa bateria. Ela também pode ser repetida com `python -m middleware.verificar`.

## Protocolo

UDP, um JSON por datagrama. Campos: `type`, `id`, `topic` e `payload`.

| type | quem envia | efeito |
| --- | --- | --- |
| create_topic / delete_topic | cliente | cria ou remove o tópico e avisa o par |
| subscribe / unsubscribe | cliente | registra o assinante neste broker |
| publish | cliente | entra na fila do tópico ou recebe erro |
| deliver | broker | entrega a publicação ao assinante |
| ack / error | broker | confirma, avisa descarte ou rejeita |
| broker_publish | broker | encaminha uma vez ao par, com o mesmo id |
| broker_hello / broker_interest | broker | sincroniza tópicos e inscrições |

Regras da publicação:

- sem inscritos nos dois brokers: descarta e confirma `discarded_no_subscribers`;
- com inscritos: entrega aos locais e encaminha ao par se ele também tiver assinantes;
- o mesmo `id` não é entregue de novo;
- fila do tópico ou fila do cliente sem vaga: erro `fila_cheia`, sem descarte silencioso;
- não há persistência: o que estava em trânsito se perde se o processo cair.

## Onde está cada coisa

- `middleware/broker.py` — os dois processos ativos
- `middleware/client.py` — conexão, publicação, inscrição e troca de broker
- `middleware/protocol.py` — o JSON e a decisão de aceitar, descartar ou rejeitar
- `jogo/` — a banca em Django, separada do transporte
