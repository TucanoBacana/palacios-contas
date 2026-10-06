(function () {
  "use strict";

  var $ = function (sel, raiz) { return (raiz || document).querySelector(sel); };
  var $$ = function (sel, raiz) { return Array.prototype.slice.call((raiz || document).querySelectorAll(sel)); };
  var semAcento = function (s) { return s.normalize("NFD").replace(/[̀-ͯ]/g, "").toLowerCase(); };
  var moeda = function (v) { return v.toLocaleString("pt-BR", { style: "currency", currency: "BRL" }); };

  /* ---------- avisos somem sozinhos ---------- */
  $$(".toast").forEach(function (el) {
    var fechar = function () {
      el.classList.add("saindo");
      setTimeout(function () { el.remove(); }, 320);
    };
    setTimeout(fechar, el.classList.contains("toast-erro") ? 9000 : 4500);
    el.addEventListener("click", fechar);
  });

  /* ---------- menu "Mais" (celular) ---------- */
  var toggleMais = $("#mais-toggle");
  if (toggleMais) {
    var rotulo = $('label[for="mais-toggle"][role="button"]');
    if (rotulo) {
      rotulo.addEventListener("keydown", function (e) {
        if (e.key === "Enter" || e.key === " ") {
          e.preventDefault();
          toggleMais.checked = !toggleMais.checked;
        }
      });
    }
    document.addEventListener("keydown", function (e) {
      if (e.key === "Escape") toggleMais.checked = false;
    });
  }

  /* ---------- evita registrar duas vezes (duplo toque) ---------- */
  document.addEventListener("submit", function (e) {
    var form = e.target;
    if (!(form instanceof HTMLFormElement) || form.method.toLowerCase() !== "post") return;
    setTimeout(function () {
      if (e.defaultPrevented) return;
      $$('button[type="submit"]').forEach(function (b) {
        if (b.form === form) {
          b.disabled = true;
          b.setAttribute("aria-busy", "true");
        }
      });
    }, 0);
  });
  window.addEventListener("pageshow", function (e) {
    if (!e.persisted) return;
    $$('button[type="submit"]:disabled').forEach(function (b) {
      b.disabled = false;
      b.removeAttribute("aria-busy");
    });
  });

  /* ---------- busca instantanea em listas ---------- */
  $$("[data-filtro]").forEach(function (campo) {
    var alvo = $(campo.getAttribute("data-filtro"));
    var vazio = campo.getAttribute("data-vazio") ? $(campo.getAttribute("data-vazio")) : null;
    if (!alvo) return;
    var itens = $$("[data-busca]", alvo);
    campo.addEventListener("keydown", function (e) {
      if (e.key === "Enter" && campo.hasAttribute("data-sem-enviar")) e.preventDefault();
    });
    campo.addEventListener("input", function () {
      var q = semAcento(campo.value.trim());
      var achou = 0;
      itens.forEach(function (el) {
        var ok = !q || semAcento(el.getAttribute("data-busca")).indexOf(q) !== -1;
        el.hidden = !ok;
        if (ok) achou++;
      });
      if (vazio) vazio.hidden = achou > 0;
    });
  });

  /* ---------- botao (i): lucro por venda e resumo do estoque ---------- */
  var pop = document.createElement("div");
  pop.className = "info-pop";
  pop.setAttribute("role", "dialog");
  pop.hidden = true;
  document.body.appendChild(pop);
  var aberto = null;

  function linha(rotulo, valor, classe) {
    var l = document.createElement("div");
    if (classe) l.className = classe;
    var dt = document.createElement("dt");
    dt.textContent = rotulo;
    var dd = document.createElement("dd");
    dd.textContent = valor;
    l.appendChild(dt);
    l.appendChild(dd);
    return l;
  }
  function pct(v) { return v.toLocaleString("pt-BR", { maximumFractionDigits: 1 }) + "%"; }

  function preencherProduto(b) {
    var preco = parseFloat(b.getAttribute("data-preco")) || 0;
    var custo = parseFloat(b.getAttribute("data-custo")) || 0;
    var estoque = parseFloat(b.getAttribute("data-estoque")) || 0;
    var titulo = document.createElement("strong");
    titulo.textContent = b.getAttribute("data-nome");
    pop.appendChild(titulo);
    if (custo <= 0) {
      var aviso = document.createElement("p");
      aviso.className = "info-nota";
      aviso.textContent = "Preço de venda " + moeda(preco) + ". Este produto está sem custo, então não dá para calcular o lucro. ";
      var link = document.createElement("a");
      link.href = "/custos";
      link.textContent = "Preencher custos";
      aviso.appendChild(link);
      pop.appendChild(aviso);
      return;
    }
    var lucro = preco - custo;
    var dl = document.createElement("dl");
    dl.className = "info-dados";
    dl.appendChild(linha("Preço de venda", moeda(preco)));
    dl.appendChild(linha("Custo", moeda(custo)));
    dl.appendChild(linha("Lucro por venda", moeda(lucro), "info-lucro"));
    dl.appendChild(linha("Margem", preco > 0 ? pct(lucro / preco * 100) : "-"));
    dl.appendChild(linha("Markup sobre o custo", pct(lucro / custo * 100)));
    pop.appendChild(dl);
    if (estoque > 0) {
      var est = document.createElement("p");
      est.className = "info-nota";
      est.textContent = "Em estoque: " + estoque.toLocaleString("pt-BR") + ". Se vender tudo, lucro de " + moeda(lucro * estoque) +
        " (custou " + moeda(custo * estoque) + ").";
      pop.appendChild(est);
    }
    if (b.getAttribute("data-estimado") === "1") {
      var aprox = document.createElement("p");
      aprox.className = "info-nota";
      aprox.textContent = "A quantidade em estoque é estimada: confira a contagem na tela Estoque.";
      pop.appendChild(aprox);
    }
  }

  function abrirInfo(b) {
    pop.textContent = "";
    var alvo = b.getAttribute("data-info-alvo");
    var modelo = alvo ? $(alvo) : null;
    if (modelo) pop.appendChild(modelo.content.cloneNode(true));
    else preencherProduto(b);
    pop.hidden = false;
    b.setAttribute("aria-expanded", "true");
    aberto = b;
    var r = b.getBoundingClientRect();
    var largura = pop.offsetWidth;
    var esquerda = Math.min(Math.max(12, r.left + r.width / 2 - largura / 2), window.innerWidth - largura - 12);
    var topo = r.bottom + 8;
    if (topo + pop.offsetHeight > window.innerHeight - 12) topo = Math.max(12, r.top - pop.offsetHeight - 8);
    pop.style.left = esquerda + "px";
    pop.style.top = topo + "px";
  }
  function fecharInfo() {
    if (!aberto) return;
    pop.hidden = true;
    aberto.setAttribute("aria-expanded", "false");
    aberto = null;
  }
  document.addEventListener("click", function (e) {
    var b = e.target.closest(".info-btn");
    if (b) {
      e.preventDefault();
      e.stopPropagation();
      var mesmo = aberto === b;
      fecharInfo();
      if (!mesmo) abrirInfo(b);
      return;
    }
    if (!e.target.closest(".info-pop")) fecharInfo();
  }, true);
  document.addEventListener("keydown", function (e) { if (e.key === "Escape") fecharInfo(); });
  window.addEventListener("scroll", fecharInfo, { passive: true });
  window.addEventListener("resize", fecharInfo);

  /* ---------- copiar e enviar mensagem de cobranca ---------- */
  $$("[data-copiar]").forEach(function (botao) {
    botao.addEventListener("click", function () {
      var campo = $(botao.getAttribute("data-copiar"));
      if (!campo) return;
      var avisar = function () {
        var antes = botao.innerHTML;
        botao.textContent = "Copiado";
        setTimeout(function () { botao.innerHTML = antes; }, 1800);
      };
      if (navigator.clipboard && navigator.clipboard.writeText) {
        navigator.clipboard.writeText(campo.value).then(avisar, function () { campo.select(); });
      } else {
        campo.select();
        try { document.execCommand("copy"); avisar(); } catch (err) { /* o texto fica selecionado */ }
      }
    });
  });
  $$("[data-compartilhar]").forEach(function (botao) {
    botao.addEventListener("click", function () {
      var campo = $(botao.getAttribute("data-compartilhar"));
      if (!campo) return;
      if (navigator.share) {
        navigator.share({ text: campo.value }).catch(function () { /* cancelou */ });
      } else {
        window.open("https://wa.me/?text=" + encodeURIComponent(campo.value), "_blank", "noopener");
      }
    });
  });

  /* ---------- app instalavel: service worker ---------- */
  if ("serviceWorker" in navigator) {
    window.addEventListener("load", function () {
      navigator.serviceWorker.register("/sw.js").catch(function () { /* sem suporte: segue normal */ });
    });
  }

  /* ---------- comanda (venda / encomenda) ---------- */
  var form = $("#form-venda");
  if (!form) return;

  var categoria = (form.closest(".venda") || document.body).getAttribute("data-cat") || "mercadinho";
  var chave = "palacios:comanda:" + categoria;
  var tiles = $$(".tile", form);
  var comanda = $("#comanda");
  var rotuloQtd = $("#cmd-qtd");
  var rotuloTotal = $("#cmd-total");
  var itensInput = $("#itens-input");
  var avisoErro = $("#venda-erro");
  var pessoa = $("#pessoa");
  var carrinho = {}; // id -> quantidade

  function guardar() {
    try {
      sessionStorage.setItem(chave, JSON.stringify({ pessoa: pessoa ? pessoa.value : "", itens: carrinho }));
    } catch (err) { /* modo privado: segue sem guardar */ }
  }
  function esquecer() {
    try { sessionStorage.removeItem(chave); } catch (err) { /* ignora */ }
  }

  function pintar(tile) {
    var id = tile.getAttribute("data-id");
    var qtd = carrinho[id] || 0;
    $(".tile-qtd", tile).textContent = qtd;
    $(".tile-passo", tile).hidden = qtd === 0;
    tile.classList.toggle("selecionado", qtd > 0);
  }

  function resumir() {
    var itens = 0;
    var total = 0;
    tiles.forEach(function (tile) {
      var qtd = carrinho[tile.getAttribute("data-id")] || 0;
      itens += qtd;
      total += qtd * parseFloat(tile.getAttribute("data-preco"));
    });
    comanda.hidden = itens === 0;
    rotuloQtd.textContent = itens + (itens === 1 ? " item" : " itens");
    rotuloTotal.textContent = moeda(total);
    if (itens > 0 && avisoErro) avisoErro.hidden = true;
    guardar();
  }

  function mudar(tile, delta) {
    var id = tile.getAttribute("data-id");
    var nova = Math.max(0, (carrinho[id] || 0) + delta);
    if (nova === 0) delete carrinho[id]; else carrinho[id] = nova;
    if (navigator.vibrate) navigator.vibrate(8);
    pintar(tile);
    resumir();
  }

  var grade = $(".tiles", form);
  grade.addEventListener("click", function (e) {
    var botao = e.target.closest("button");
    var tile = e.target.closest(".tile");
    if (!botao || !tile) return;
    mudar(tile, botao.getAttribute("data-acao") === "menos" ? -1 : 1);
  });

  var limpar = $("#cmd-limpar");
  if (limpar) {
    limpar.addEventListener("click", function () {
      carrinho = {};
      tiles.forEach(pintar);
      resumir();
    });
  }

  if (pessoa) pessoa.addEventListener("input", guardar);

  form.addEventListener("submit", function (e) {
    var lista = Object.keys(carrinho).map(function (id) {
      return { produto_id: id, quantidade: carrinho[id] };
    });
    if (lista.length === 0) {
      e.preventDefault();
      if (avisoErro) {
        avisoErro.hidden = false;
        avisoErro.scrollIntoView({ behavior: "smooth", block: "center" });
      }
      return;
    }
    itensInput.value = JSON.stringify(lista);
  });

  /* Se acabou de registrar com sucesso, comeca limpo. Se deu erro, devolve o que estava montado. */
  var houveSucesso = $(".toast:not(.toast-erro)");
  if (houveSucesso) {
    esquecer();
  } else {
    try {
      var salvo = JSON.parse(sessionStorage.getItem(chave) || "null");
      if (salvo) {
        if (pessoa && !pessoa.value && salvo.pessoa) pessoa.value = salvo.pessoa;
        tiles.forEach(function (tile) {
          var qtd = salvo.itens && salvo.itens[tile.getAttribute("data-id")];
          if (qtd > 0) carrinho[tile.getAttribute("data-id")] = qtd;
          pintar(tile);
        });
        resumir();
      }
    } catch (err) { /* ignora estado invalido */ }
  }
})();
