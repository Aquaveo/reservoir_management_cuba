// mapa.js - mapa de la portada. Un circulo por embalse, coloreado por el
// llenado actual respecto al volumen autorizado.
"use strict";

(function () {
  const el = document.getElementById("mapa-datos");
  if (!el || !window.L) return;
  const M = JSON.parse(el.textContent);

  const mapa = L.map("mapa", { scrollWheelZoom: false }).setView([21.5, -79.0], 7);
  L.tileLayer(M.teselas, { maxZoom: 18, attribution: M.atribucion }).addTo(mapa);

  // misma escala que la lista: menos de 30% bajo, 30-80% normal, mas de 80% alto
  const color = p => p === null || p === undefined ? "#8a99a6" : p < 30 ? "#b45309" : p <= 80 ? "#0b6e8f" : "#1e5b8f";
  const fmt = (x, n) => (x === null || x === undefined) ? "–" : Number(x).toFixed(n);

  const puntos = [];
  for (const e of M.embalses) {
    const c = color(e.pct);
    const m = L.circleMarker([e.lat, e.lon], {
      radius: 9, weight: 2, color: "#fff", fillColor: c, fillOpacity: 0.95,
    }).addTo(mapa);
    let html = "<div class='popup'><b>" + e.nombre + "</b><br><span class='prov'>" + e.provincia + "</span>";
    if (e.hay) {
      html += "<br>Último dato " + e.f0 + ": <b>" + fmt(e.nivel0, 2) + " m</b>" +
              (e.pct !== null && e.pct !== undefined ? " (" + fmt(e.pct, 0) + "% del autorizado)" : "") +
              "<br>En 14 días (p50): <b>" + fmt(e.nivel_fin, 2) + " m</b>" +
              (e.vierten ? "<br>Miembros que vierten: " + e.vierten : "");
    } else {
      html += "<br>Sin pronóstico todavía.";
    }
    html += "<br><a href='" + e.url + "'>Ver el embalse</a></div>";
    m.bindPopup(html);
    m.bindTooltip(e.nombre, { direction: "top", offset: [0, -8] });
    puntos.push([e.lat, e.lon]);
  }
  if (puntos.length) mapa.fitBounds(puntos, { padding: [30, 30], maxZoom: 9 });
})();
