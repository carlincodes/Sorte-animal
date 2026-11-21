function token() {
  const campo = document.querySelector("[name=csrfmiddlewaretoken]");
  return campo ? campo.value : "";
}

async function postar(url, corpo) {
  const resposta = await fetch(url, {
    method: "POST",
    headers: { "Content-Type": "application/json", "X-CSRFToken": token() },
    body: JSON.stringify(corpo || {}),
  });
  return resposta.json();
}

function escapar(texto) {
  return String(texto ?? "").replace(/[&<>"']/g, (caractere) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", "\"": "&quot;", "'": "&#39;",
  }[caractere]));
}

function digitando() {
  const ativo = document.activeElement;
  return ativo && (ativo.tagName === "INPUT" || ativo.tagName === "TEXTAREA");
}

function desenharPlacar(estado) {
  const lista = document.getElementById("placar");
  const presentes = document.getElementById("presentes");
  if (presentes) presentes.textContent = estado.presentes ? `${estado.presentes} na sala` : "";
  if (!lista || !estado.placar) return;
  lista.innerHTML = estado.placar.length
    ? estado.placar.map((item) => `<li>${escapar(item.nome)} · ${item.pontos}</li>`).join("")
    : "<li class='miudo'>A sala ainda está vazia.</li>";
}

function iniciarSala() {
  const codigo = document.body.dataset.codigo;
  const aviso = document.getElementById("aviso");
  document.querySelectorAll(".cartao-animal").forEach((cartao) => {
    cartao.addEventListener("click", () => {
      document.querySelectorAll(".cartao-animal").forEach((item) => item.classList.remove("escolhido"));
      cartao.classList.add("escolhido");
      const confirmar = document.getElementById("confirmar");
      confirmar.disabled = false;
      confirmar.dataset.opcao = cartao.dataset.opcao;
    });
  });
  const confirmar = document.getElementById("confirmar");
  if (confirmar) {
    confirmar.addEventListener("click", async () => {
      confirmar.disabled = true;
      const dados = await postar(`/api/sala/${codigo}/apostar/`, { opcao: confirmar.dataset.opcao });
      if (aviso) aviso.textContent = dados.erro || dados.mensagem || "";
      if (dados.ok) location.reload();
      else confirmar.disabled = false;
    });
  }
  observar(codigo);
}

function iniciarPainel() {
  const codigo = document.body.dataset.codigo;
  const aviso = document.getElementById("aviso");
  const copiar = document.getElementById("copiar");
  if (copiar) {
    copiar.addEventListener("click", async () => {
      try {
        await navigator.clipboard.writeText(copiar.dataset.link);
        copiar.textContent = "Link copiado";
      } catch (erro) {
        copiar.textContent = copiar.dataset.link;
      }
    });
  }
  document.querySelectorAll("[data-abrir]").forEach((botao) => {
    botao.addEventListener("click", () => acao(`/api/sala/${codigo}/abrir/`, { jogada: botao.dataset.abrir }, botao));
  });
  const sortear = document.getElementById("sortear");
  if (sortear) sortear.addEventListener("click", () => acao(`/api/sala/${codigo}/sortear/`, {}, sortear));

  async function acao(url, corpo, botao) {
    botao.disabled = true;
    const dados = await postar(url, corpo);
    if (aviso) aviso.textContent = dados.erro || dados.mensagem || "";
    if (dados.ok) location.reload();
    else botao.disabled = false;
  }
  observar(codigo);
}

function observar(codigo) {
  const pagina = document.body.dataset.pagina;
  setInterval(async () => {
    try {
      const estado = await fetch(`/api/sala/${codigo}/estado/`).then((resposta) => resposta.json());
      desenharPlacar(estado);
      const broker = document.getElementById("broker");
      if (broker) broker.textContent = estado.conectado ? `Middleware em ${estado.broker}` : "Sem broker no momento";
      const apostas = document.getElementById("apostas");
      if (apostas) {
        apostas.innerHTML = estado.apostas.length
          ? estado.apostas.map((item) => `<li>${escapar(item.nome)} escolheu ${escapar(item.animal)}</li>`).join("")
          : "<li class='miudo'>Ninguém escolheu ainda.</li>";
      }
      const mudou = String(estado.jogada_id || "") !== (document.body.dataset.jogada || "")
        || estado.status !== document.body.dataset.status;
      if (mudou && !digitando()) location.reload();
    } catch (erro) {
      console.error(erro);
    }
  }, 1200);
}

function iniciarProfessor() {
  const lista = document.getElementById("testes");
  const andamento = document.getElementById("andamento");
  const botao = document.getElementById("rodar");
  const nomes = { pronto: "pronto", na_fila: "na fila", rodando: "rodando", ok: "passou", falhou: "falhou" };

  function desenhar(dados) {
    botao.disabled = dados.rodando;
    botao.textContent = dados.rodando ? "Aplicando…" : "Aplicar os testes";
    andamento.textContent = dados.rodando ? `Agora: ${dados.atual}` : "";
    lista.innerHTML = dados.secoes.map((secao) => `
      <article class="teste">
        <header>
          <h2>${escapar(secao.titulo)}</h2>
          <span class="selo ${escapar(secao.status)}">${nomes[secao.status] || escapar(secao.status)}</span>
        </header>
        <p>${escapar(secao.texto)}</p>
        ${secao.detalhe ? `<p class="miudo">${escapar(secao.detalhe)}</p>` : ""}
        ${secao.ms ? `<p class="miudo">${secao.ms} ms</p>` : ""}
        <ul class="passos">
          ${(secao.passos || []).map((passo) => `<li class="${passo.ok ? "ok" : "falhou"}">${escapar(passo.titulo)}${passo.detalhe ? " — " + escapar(passo.detalhe) : ""}</li>`).join("")}
        </ul>
      </article>
    `).join("");
  }

  async function atualizar() {
    desenhar(await fetch("/api/professor/").then((resposta) => resposta.json()));
  }
  botao.addEventListener("click", async () => {
    botao.disabled = true;
    const dados = await postar("/api/professor/rodar/", {});
    if (!dados.ok && andamento) andamento.textContent = dados.erro || "";
    atualizar();
  });
  atualizar();
  setInterval(atualizar, 1000);
}

const pagina = document.body.dataset.pagina;
if (pagina === "sala") iniciarSala();
if (pagina === "painel" && document.getElementById("codigo")) iniciarPainel();
if (pagina === "professor") iniciarProfessor();
