// embalse.js - pagina de un embalse en la app: pronostico (pagina.js, igual que
// la pagina de pronosticar.py) e historico de la serie limpia.
"use strict";

(function () {
  const leer = id => { const e = document.getElementById(id); return e ? JSON.parse(e.textContent) : null; };
  const D = leer("datos-pronostico");
  const H = leer("datos-historia");
  if (!D) return;

  iniciarPronostico(D);

  // Plotly dibuja con ancho cero dentro de una pestana oculta: se redimensiona al mostrarla.
  let histDibujado = false;
  document.getElementById("tab-pron-btn").addEventListener("shown.bs.tab", () => Plotly.Plots.resize("grafico"));
  document.getElementById("tab-hist-btn").addEventListener("shown.bs.tab", () => {
    if (!histDibujado) { dibujarHistorico(); histDibujado = true; }
    else Plotly.Plots.resize("graficoHist");
  });

  function dibujarHistorico() {
    const x = H.fecha;
    const ultimo = x[x.length - 1];
    const tr = [
      { x, y: H.nivel, mode: "lines", line: { color: "#0b6e8f", width: 1.4 }, name: "Nivel", yaxis: "y",
        hovertemplate: "%{x}<br>%{y:.2f} m<extra></extra>" },
      { x, y: H.volumen, mode: "lines", line: { color: "#3d7d3a", width: 1.4 }, name: "Volumen", yaxis: "y2",
        hovertemplate: "%{x}<br>%{y:.1f} hm³<extra></extra>" },
      { x, y: H.Qin_cms, mode: "lines", line: { color: "#7a4fa3", width: 1 }, name: "Caudal de entrada", yaxis: "y3",
        hovertemplate: "%{x}<br>%{y:.1f} m³/s<extra></extra>" },
    ];
    const colores = ["#9b2c2c", "#5b6b7a", "#b7791f", "#2b6cb0"];
    const shapes = [], notas = [];
    H.referencias.forEach((r, i) => {
      shapes.push({ type: "line", xref: "paper", x0: 0, x1: 1, yref: "y", y0: r.nivel, y1: r.nivel,
                    line: { color: colores[i % colores.length], width: 1, dash: "dot" } });
      notas.push({ xref: "paper", x: 1, xanchor: "right", yref: "y", y: r.nivel, yanchor: "bottom",
                   text: r.nombre + " " + r.nivel.toFixed(2) + " m", showarrow: false,
                   font: { size: 11, color: colores[i % colores.length] } });
    });
    const desde = new Date(ultimo); desde.setFullYear(desde.getFullYear() - 3);
    const layout = {
      margin: { t: 10, r: 16, b: 40, l: 64 }, showlegend: false, hovermode: "x",
      font: { family: "Segoe UI, Calibri, Arial, sans-serif", size: 12, color: "#1d2733" },
      xaxis: { anchor: "y3", range: [desde.toISOString().slice(0, 10), ultimo], gridcolor: "#eee",
               rangeselector: { buttons: [
                 { count: 3, label: "3 meses", step: "month", stepmode: "backward" },
                 { count: 1, label: "1 año", step: "year", stepmode: "backward" },
                 { count: 3, label: "3 años", step: "year", stepmode: "backward" },
                 { step: "all", label: "Todo" }] },
               rangeslider: { visible: false } },
      yaxis: { domain: [0.56, 1], title: { text: "Nivel (m)" }, gridcolor: "#eee" },
      yaxis2: { domain: [0.29, 0.52], title: { text: "Volumen (hm³)" }, gridcolor: "#eee" },
      yaxis3: { domain: [0, 0.25], title: { text: "Qin (m³/s)" }, gridcolor: "#eee" },
      shapes, annotations: notas, plot_bgcolor: "#fff", paper_bgcolor: "#fff",
    };
    Plotly.react("graficoHist", tr, layout, { displaylogo: false, responsive: true,
      toImageButtonOptions: { filename: D.embalse + "_historico" } });
  }

  document.getElementById("descargarHist").addEventListener("click", () => {
    const cols = ["fecha", "nivel", "volumen", "Qin_cms", "entrega", "desembalse", "vertimiento", "calidad"];
    const filas = [cols.join(",")];
    for (let i = 0; i < H.fecha.length; i++)
      filas.push(cols.map(c => { const v = H[c][i]; return v === null || v === undefined ? "" : String(v); }).join(","));
    const blob = new Blob([filas.join("\n") + "\n"], { type: "text/csv" });
    const a = document.createElement("a");
    a.href = URL.createObjectURL(blob);
    a.download = D.embalse + "_diario_limpio.csv";
    document.body.appendChild(a); a.click(); a.remove();
    setTimeout(() => URL.revokeObjectURL(a.href), 2000);
  });
})();
