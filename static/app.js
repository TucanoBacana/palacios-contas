(function () {
  // ---- tela de venda (mercadinho / restaurante): carrinho por toque ----
  const grid = document.querySelector(".grid-produtos");
  if (grid) {
    const cart = {}; // produto_id -> {nome, preco, qtd}
    const barra = document.getElementById("barra-carrinho");
    const bcQtd = document.getElementById("bc-qtd");
    const bcTotal = document.getElementById("bc-total");
    const itensInput = document.getElementById("itens-input");
    const form = document.getElementById("form-venda");

    function formatarMoeda(v) {
      return "R$ " + v.toFixed(2).replace(".", ",");
    }

    function atualizarResumo() {
      let qtdTotal = 0;
      let valorTotal = 0;
      for (const id in cart) {
        qtdTotal += cart[id].qtd;
        valorTotal += cart[id].qtd * cart[id].preco;
      }
      if (qtdTotal > 0) {
        barra.hidden = false;
        bcQtd.textContent = qtdTotal + (qtdTotal === 1 ? " item" : " itens");
        bcTotal.textContent = formatarMoeda(valorTotal);
      } else {
        barra.hidden = true;
      }
    }

    function atualizarCard(card, id) {
      const stepper = card.querySelector(".cp-stepper");
      const qtdEl = card.querySelector(".cp-qtd");
      const qtd = cart[id] ? cart[id].qtd : 0;
      qtdEl.textContent = qtd;
      if (qtd > 0) {
        stepper.hidden = false;
        card.classList.add("selecionado");
      } else {
        stepper.hidden = true;
        card.classList.remove("selecionado");
      }
    }

    grid.addEventListener("click", function (ev) {
      const card = ev.target.closest(".card-produto");
      if (!card) return;
      const id = card.dataset.id;
      const nome = card.dataset.nome;
      const preco = parseFloat(card.dataset.preco);
      if (!cart[id]) cart[id] = { nome: nome, preco: preco, qtd: 0 };

      const acaoEl = ev.target.closest("[data-acao]");
      const acao = acaoEl ? acaoEl.dataset.acao : "mais";

      if (acao === "menos") {
        cart[id].qtd = Math.max(0, cart[id].qtd - 1);
      } else {
        cart[id].qtd += 1;
      }
      if (cart[id].qtd === 0) delete cart[id];

      atualizarCard(card, id);
      atualizarResumo();
    });

    if (form) {
      form.addEventListener("submit", function (ev) {
        const itens = Object.keys(cart).map(function (id) {
          return { produto_id: id, quantidade: cart[id].qtd };
        });
        if (itens.length === 0) {
          ev.preventDefault();
          alert("Toque em pelo menos um produto antes de registrar.");
          return;
        }
        itensInput.value = JSON.stringify(itens);
      });
    }
  }
})();
