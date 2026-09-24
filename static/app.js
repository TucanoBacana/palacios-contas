(function () {
  const produtoSelect = document.getElementById("produto_id");
  const quantidadeInput = document.getElementById("quantidade");
  const valorOutput = document.getElementById("valor-total");

  if (!produtoSelect || !quantidadeInput || !valorOutput) return;

  function atualizarValor() {
    const opt = produtoSelect.options[produtoSelect.selectedIndex];
    const preco = parseFloat(opt ? opt.dataset.preco : "0") || 0;
    const qtd = parseFloat(quantidadeInput.value) || 0;
    const total = preco * qtd;
    valorOutput.textContent = "R$ " + total.toFixed(2).replace(".", ",");
  }

  produtoSelect.addEventListener("change", atualizarValor);
  quantidadeInput.addEventListener("input", atualizarValor);
  atualizarValor();
})();
