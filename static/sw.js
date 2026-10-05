/* Service worker do PALACIO'S.
   - Arquivos fixos (css, js, icones, fontes): respondem na hora do cache e se atualizam por tras.
   - Paginas: sempre da internet, porque as contas precisam estar em dia. Sem internet, mostra /offline.
   Nunca guarda paginas com dados de contas, nem respostas de formularios. */
var VERSAO = "v1";
var ESTATICOS = "palacios-estaticos-" + VERSAO;
var PRE_CACHE = ["/offline", "/static/style.css", "/static/app.js", "/static/icon.svg", "/static/icon-192.png"];

self.addEventListener("install", function (e) {
  e.waitUntil(
    caches.open(ESTATICOS).then(function (cache) {
      return Promise.all(PRE_CACHE.map(function (url) {
        return cache.add(url).catch(function () { /* um arquivo faltando nao impede de instalar */ });
      }));
    }).then(function () { return self.skipWaiting(); })
  );
});

self.addEventListener("activate", function (e) {
  e.waitUntil(
    caches.keys().then(function (nomes) {
      return Promise.all(nomes.filter(function (n) { return n !== ESTATICOS; })
        .map(function (n) { return caches.delete(n); }));
    }).then(function () { return self.clients.claim(); })
  );
});

function atualizandoPorTras(req) {
  return caches.open(ESTATICOS).then(function (cache) {
    return cache.match(req).then(function (guardado) {
      var rede = fetch(req).then(function (resp) {
        if (resp && (resp.ok || resp.type === "opaque")) cache.put(req, resp.clone());
        return resp;
      }).catch(function () { return guardado; });
      return guardado || rede;
    });
  });
}

self.addEventListener("fetch", function (e) {
  var req = e.request;
  if (req.method !== "GET") return;
  var url = new URL(req.url);

  if (req.mode === "navigate") {
    e.respondWith(fetch(req).catch(function () { return caches.match("/offline"); }));
    return;
  }
  var mesmaOrigem = url.origin === self.location.origin;
  if ((mesmaOrigem && url.pathname.indexOf("/static/") === 0) ||
      url.hostname === "fonts.googleapis.com" || url.hostname === "fonts.gstatic.com") {
    e.respondWith(atualizandoPorTras(req));
  }
});
