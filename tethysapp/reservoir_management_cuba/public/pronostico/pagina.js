// pagina.js - la pagina interactiva del pronostico de un embalse.
// La usan la pagina que escribe pronosticar.py y la app Tethys. Necesita
// Plotly y nucleo.js cargados antes, y el marcado de plantilla.html.
"use strict";

function iniciarPronostico(D) {
  const E = crearEmbalse(D);
  const N = D.fechas.length;
  const HAY_ALIVIADERO = !!D.vertedero;
  const BASE = {
    ent: D.base.entrega.map(x => x === null ? 0 : x),
    des: D.base.desembalse.map(x => x === null ? 0 : x),
    ver: D.base.vertimiento.map(x => x === null ? NaN : x),   // NaN = aliviadero
  };
  const COLOR = { entrega: "#1f77b4", desembalse: "#ff7f0e", vertimiento: "#2ca02c" };
  const fmt = (x, n) => Number.isFinite(x) ? x.toFixed(n) : "–";
  const fechaCorta = s => { const [a, m, d] = s.split("-"); return d + "-" + ["ene","feb","mar","abr","may","jun","jul","ago","sep","oct","nov","dic"][+m - 1]; };

  // estado de cada version: cada una tiene su propia tabla de salidas, como en el Colab
  const ESTADO = {};
  for (const v of D.versiones) {
    ESTADO[v.clave] = { modoED: "auto", modoV: "auto", ent: BASE.ent.slice(), des: BASE.des.slice(),
                        ver: BASE.ver.map(x => Number.isFinite(x) ? x : 0), vista: "stats", ultimo: null };
  }
  let actual = D.versiones[D.versiones.length - 1].clave;
  let hayCambios = false;     // hay salidas editadas que no se han descargado

  function entradas(st) {
    const ent = st.modoED === "manual" ? st.ent : BASE.ent;
    const des = st.modoED === "manual" ? st.des : BASE.des;
    const ver = st.modoV === "manual" ? st.ver : BASE.ver;
    return { ent, des, ver };
  }

  function calcular(clave) {
    const st = ESTADO[clave];
    const { ent, des, ver } = entradas(st);
    const R = propagar(E, D.Q[clave], D.v0, ent, des, ver, D.evap_mm);
    const filas = tablaBalance(D.fechas, D.Q[clave], R, ent, des);
    st.ultimo = { R, filas, ent, des, ver };
    return st.ultimo;
  }

  // ------------------------------------------------------------ encabezado
  function encabezado() {
    const poner = (id, t) => { const e = document.getElementById(id); if (e) e.textContent = t; };
    poner("titulo", "Embalse " + D.embalse + " — pronóstico de nivel y volumen");
    poner("subtitulo",
      "GEOGLOWS emitido el " + D.emision + " · " + N + " días (" + D.fechas[0] + " a " + D.fechas[N - 1] + ") · día de balance de 08:00 a 08:00, hora de Cuba");
    const ar = D.info.ar;
    const filas = [
      ["Condición inicial", D.f0 + " " + (D.hora0 || "").slice(0, 5) + " · nivel " + fmt(D.nivel0, 2) + " m · volumen " + fmt(D.v0, 2) + " hm³"],
      ["Nivel muerto / techo", fmt(D.v_min, 1) + " hm³ (" + fmt(E.nivelDeVol(D.v_min), 2) + " m) / " + fmt(D.v_max, 1) + " hm³ (" + fmt(E.nivelDeVol(D.v_max), 2) + " m)"],
      ["Aliviadero", D.info.vertedero],
      ["Curva nivel–volumen", D.info.curva],
      ["Ajuste de condiciones iniciales", ar.mu !== undefined
        ? "AR(1) con " + ar.pares + " días: μ " + fmt(ar.mu, 2) + ", φ " + fmt(ar.phi, 3) + ", error actual " + fmt(ar.e_now, 2) + " m³/s"
        : "no se aplicó (" + ar.pares + " días de error)"],
      ["Retrospectiva GEOGLOWS", D.info.retro],
    ];
    document.getElementById("datos").innerHTML = filas.map(([k, v]) => "<div><b>" + k + ":</b> " + v + "</div>").join("");
    document.getElementById("avisos").innerHTML = D.info.avisos.map(a => "<div>" + a + "</div>").join("");
    poner("pie", "Página generada por pronosticar.py el " + D.generado + ".");
    document.getElementById("modoVauto").textContent = HAY_ALIVIADERO ? "Curva de gasto" : "Persistencia";
    document.getElementById("descargar").textContent = "Descargar " + D.embalse + "_pronostico_" + D.sello + ".zip";
    document.getElementById("notaTabla").innerHTML =
      "<b>Entrega</b> y <b>desembalse</b> en hm³/día; <b>vertimiento</b> en m³/s, como los da el portal (1 hm³/día = 11.57 m³/s). " +
      "En <i>Persistencia</i>" + (HAY_ALIVIADERO ? " o <i>Curva de gasto</i>" : "") + " las casillas no se editan y muestran lo que usa el modelo " +
      "(el vertimiento, lo que resolvió el balance, mediana del ensamble). En <i>Manual</i> manda lo que se escriba; " +
      "el pronóstico se recalcula al salir de la casilla o con Enter. Una casilla en rojo es una salida que el embalse no puede dar entera: " +
      "llegaría al nivel muerto y se recorta.";
    const p = document.getElementById("pestanas");
    for (const v of D.versiones) {
      const b = document.createElement("button");
      b.textContent = v.titulo; b.dataset.v = v.clave;
      b.onclick = () => { actual = v.clave; dibujarTodo(); };
      p.appendChild(b);
    }
  }

  // ------------------------------------------------------------ tabla de salidas
  const CAJAS = { ent: [], des: [], ver: [] };
  function armarTabla() {
    const t = document.getElementById("tabla");
    let h = "<tr><th></th>" + D.fechas.map(f => "<th>" + fechaCorta(f) + "</th>").join("") + "</tr>";
    const filas = [["ent", "Entrega <small>hm³/día</small>"], ["des", "Desembalse <small>hm³/día</small>"], ["ver", "Vertimiento <small>m³/s</small>"]];
    for (const [k, et] of filas)
      h += "<tr><td><b>" + et + "</b></td>" + D.fechas.map((_, i) =>
        '<td><input type="number" min="0" step="0.01" data-k="' + k + '" data-i="' + i + '" aria-label="' +
        et.replace(/<[^>]+>/g, "") + ", " + fechaCorta(D.fechas[i]) + '"></td>').join("") + "</tr>";
    t.insertAdjacentHTML("beforeend", h);
    t.querySelectorAll("input").forEach(inp => {
      CAJAS[inp.dataset.k][+inp.dataset.i] = inp;
      inp.addEventListener("change", () => {
        const st = ESTADO[actual], k = inp.dataset.k, i = +inp.dataset.i;
        let v = parseFloat(inp.value);
        if (!Number.isFinite(v) || v < 0) v = 0;
        st[k][i] = v;
        hayCambios = true;
        dibujarTodo();
      });
    });
  }

  function llenarTabla() {
    const st = ESTADO[actual], u = st.ultimo;
    const manED = st.modoED === "manual", manV = st.modoV === "manual";
    for (let i = 0; i < N; i++) {
      const ce = CAJAS.ent[i], cd = CAJAS.des[i], cv = CAJAS.ver[i];
      ce.value = (manED ? st.ent[i] : BASE.ent[i]).toFixed(3);
      cd.value = (manED ? st.des[i] : BASE.des[i]).toFixed(3);
      ce.disabled = cd.disabled = !manED;
      const verMostrar = manV ? st.ver[i] : (u.filas[i].vertimiento_p50 / CMS_A_HM3);
      cv.value = Number.isFinite(verMostrar) ? verMostrar.toFixed(3) : "";
      cv.disabled = !manV;
      const f = u.filas[i];
      const rE = f.entrega_pedida - f.entrega_p50 > 1e-6, rD = f.desembalse_pedido - f.desembalse_p50 > 1e-6;
      ce.classList.toggle("recortado", rE);
      cd.classList.toggle("recortado", rD);
      ce.title = rE ? "Solo pueden salir " + fmt(f.entrega_p50, 3) + " hm³/día (mediana): el embalse llegaría al nivel muerto" : "";
      cd.title = rD ? "Solo pueden salir " + fmt(f.desembalse_p50, 3) + " hm³/día (mediana): el embalse llegaría al nivel muerto" : "";
      ce.classList.toggle("editado", manED && Math.abs(st.ent[i] - BASE.ent[i]) > 1e-9);
      cd.classList.toggle("editado", manED && Math.abs(st.des[i] - BASE.des[i]) > 1e-9);
      cv.classList.toggle("editado", manV);
    }
  }

  // ------------------------------------------------------------ grafico
  function nanmean(a) { const s = a.filter(Number.isFinite); return s.length ? s.reduce((x, y) => x + y, 0) / s.length : NaN; }
  function col(M, j) { return M.map(f => f[j]); }
  function conIni(v, y) { return [v].concat(y.map(x => Number.isFinite(x) ? x : null)); }

  function trazasBanda(M, ini, eje, leyenda, vista) {
    const x = [D.f0].concat(D.fechas), out = [];
    const nm = M[0].length;
    if (vista === "stats") {
      const mn = conIni(ini, M.map(f => nanpct(f, 0))), mx = conIni(ini, M.map(f => nanpct(f, 100)));
      const p25 = conIni(ini, M.map(f => nanpct(f, 25))), p75 = conIni(ini, M.map(f => nanpct(f, 75)));
      const xr = x.concat(x.slice().reverse());
      out.push({ x: xr, y: mx.concat(mn.slice().reverse()), fill: "toself", mode: "lines", fillcolor: "rgba(173,216,230,0.4)",
                 line: { color: "lightblue", dash: "dash", width: 1 }, name: "Máx y mín", legendgroup: "mm", showlegend: leyenda, hoverinfo: "skip", yaxis: eje });
      out.push({ x: xr, y: p75.concat(p25.slice().reverse()), fill: "toself", mode: "lines", fillcolor: "rgba(144,238,144,0.5)",
                 line: { color: "lightgreen", width: 1 }, name: "25–75%", legendgroup: "iq", showlegend: leyenda, hoverinfo: "skip", yaxis: eje });
      out.push({ x, y: conIni(ini, col(M, nm - 1)), mode: "lines", line: { color: "black", width: 1.5 }, name: "Alta resolución", legendgroup: "hr", showlegend: leyenda, yaxis: eje });
      out.push({ x, y: conIni(ini, M.map(nanmean)), mode: "lines", line: { color: "blue", width: 2 }, name: "Media", legendgroup: "me", showlegend: leyenda, yaxis: eje });
      out.push({ x, y: conIni(ini, M.map(f => nanpct(f, 50))), mode: "lines", line: { color: "red", width: 2 }, name: "Mediana", legendgroup: "md", showlegend: leyenda, yaxis: eje });
    } else {
      for (let m = 0; m < nm - 1; m++)
        out.push({ x, y: conIni(ini, col(M, m)), mode: "lines", line: { width: 1 }, opacity: 0.5, showlegend: false, hoverinfo: "skip", yaxis: eje });
      out.push({ x, y: conIni(ini, col(M, nm - 1)), mode: "lines", line: { color: "black", width: 1.8 }, name: "Alta resolución", legendgroup: "hr", showlegend: leyenda, yaxis: eje });
      out.push({ x, y: conIni(ini, M.map(f => nanpct(f, 50))), mode: "lines", line: { color: "red", width: 2 }, name: "Mediana", legendgroup: "md", showlegend: leyenda, yaxis: eje });
    }
    return out;
  }

  function dibujarGrafico() {
    const st = ESTADO[actual], { R, filas } = st.ultimo, O = D.obs;
    const tr = [].concat(trazasBanda(R.N, D.nivel0, "y", true, st.vista), trazasBanda(R.V, D.v0, "y2", false, st.vista));
    tr.push({ x: O.fecha, y: O.nivel, mode: "lines+markers", line: { color: "#333", width: 2 }, marker: { size: 6 }, name: "Observado", legendgroup: "ob", yaxis: "y" });
    tr.push({ x: O.fecha, y: O.volumen, mode: "lines+markers", line: { color: "#333", width: 2 }, marker: { size: 6 }, name: "Observado", legendgroup: "ob", showlegend: false, yaxis: "y2" });
    const obsSal = { entrega: O.entrega, desembalse: O.desembalse, vertimiento: O.vertimiento.map(v => v * CMS_A_HM3) };
    const proSal = { entrega: filas.map(f => f.entrega_p50), desembalse: filas.map(f => f.desembalse_p50), vertimiento: filas.map(f => f.vertimiento_p50) };
    for (const k of ["entrega", "desembalse", "vertimiento"]) {
      tr.push({ type: "bar", x: O.fecha, y: obsSal[k], marker: { color: COLOR[k] }, opacity: 0.45, name: k + " (obs)", showlegend: false, legendgroup: k, yaxis: "y3" });
      tr.push({ type: "bar", x: D.fechas, y: proSal[k], marker: { color: COLOR[k] }, name: k[0].toUpperCase() + k.slice(1), legendgroup: k, yaxis: "y3" });
    }
    const pedido = filas.map(f => f.entrega_pedida + f.desembalse_pedido);
    if (filas.some(f => (f.entrega_pedida - f.entrega_p50) + (f.desembalse_pedido - f.desembalse_p50) > 1e-6))
      tr.push({ x: D.fechas, y: pedido, mode: "lines+markers", line: { color: "red", dash: "dot" }, name: "Pedido (recortado por el piso)", yaxis: "y3" });
    const titulo = D.versiones.find(v => v.clave === actual).titulo;
    document.getElementById("tituloGrafico").textContent = D.embalse + " — " + titulo + " — emitido " + D.emision;
    const linea = ref => ({ type: "line", xref: "x", yref: ref + " domain", x0: D.f0, x1: D.f0, y0: 0, y1: 1, line: { color: "gray", dash: "dot", width: 1 } });
    const layout = {
      template: "plotly_white", barmode: "stack", margin: { t: 16, r: 16, b: 40, l: 64 },
      font: { family: "Segoe UI, Calibri, Arial, sans-serif", size: 12, color: "#1d2733" },
      legend: { orientation: "h", y: -0.06 },
      xaxis: { anchor: "y3", tickformat: "%d %b", showgrid: true, gridcolor: "#eee" },
      yaxis: { domain: [0.62, 1], title: { text: "Nivel (m)" }, gridcolor: "#eee" },
      yaxis2: { domain: [0.30, 0.58], title: { text: "Volumen (hm³)" }, gridcolor: "#eee" },
      yaxis3: { domain: [0, 0.26], title: { text: "Salidas (hm³/día)" }, gridcolor: "#eee" },
      shapes: [linea("y"), linea("y2"), linea("y3")],
      plot_bgcolor: "#fff", paper_bgcolor: "#fff",
    };
    Plotly.react("grafico", tr, layout, { displaylogo: false, responsive: true,
      toImageButtonOptions: { filename: D.embalse + "_" + actual + "_" + D.sello } });
  }

  function dibujarResumen() {
    const { R, filas } = ESTADO[actual].ultimo, fin = filas[N - 1];
    const nm = R.VE[0].length;
    let vierten = 0;
    for (let m = 0; m < nm; m++) { let s = 0; for (let d = 0; d < N; d++) s += Number.isFinite(R.VE[d][m]) ? R.VE[d][m] : 0; if (s > 0) vierten++; }
    const suma = k => filas.reduce((a, f) => a + f[k], 0);
    const item = (t, v) => "<div><dt>" + t + "</dt><dd>" + v + "</dd></div>";
    document.getElementById("resumen").innerHTML =
      item("Nivel el " + fechaCorta(D.fechas[N - 1]) + " (p50)", fmt(fin.nivel_p50, 2) + " m <small>p10 " + fmt(fin.nivel_p10, 2) + " · p90 " + fmt(fin.nivel_p90, 2) + " · hoy " + fmt(D.nivel0, 2) + "</small>") +
      item("Volumen el " + fechaCorta(D.fechas[N - 1]) + " (p50)", fmt(fin.volumen_p50, 1) + " hm³ <small>hoy " + fmt(D.v0, 1) + "</small>") +
      item("Miembros que vierten", vierten + " <small>de " + nm + "</small>") +
      item("Salidas en " + N + " días (p50)", fmt(suma("salida_total_p50"), 2) + " hm³ <small>entrega " + fmt(suma("entrega_p50"), 2) +
           " · desembalse " + fmt(suma("desembalse_p50"), 2) + " · vertimiento " + fmt(suma("vertimiento_p50"), 2) + "</small>");
  }

  function marcarBotones() {
    const st = ESTADO[actual];
    const marca = (b, on) => { b.classList.toggle("activa", on); b.setAttribute("aria-pressed", on ? "true" : "false"); };
    document.querySelectorAll("#pestanas button").forEach(b => marca(b, b.dataset.v === actual));
    for (const [id, val] of [["modoED", st.modoED], ["modoV", st.modoV], ["vista", st.vista]])
      document.querySelectorAll("#" + id + " button").forEach(b => marca(b, b.dataset.v === val));
  }

  function dibujarTodo() {
    calcular(actual);
    marcarBotones();
    llenarTabla();
    dibujarResumen();
    dibujarGrafico();
  }

  // ------------------------------------------------------------ controles
  function segmento(id, campo, alCambiar) {
    document.querySelectorAll("#" + id + " button").forEach(b => b.addEventListener("click", () => {
      const st = ESTADO[actual];
      if (st[campo] === b.dataset.v) return;
      if (alCambiar) alCambiar(st, b.dataset.v);
      st[campo] = b.dataset.v;
      if (campo !== "vista") hayCambios = true;
      dibujarTodo();
    }));
  }
  segmento("modoED", "modoED", (st, v) => { if (v === "manual" && st.modoED === "auto") { st.ent = BASE.ent.slice(); st.des = BASE.des.slice(); } });
  segmento("modoV", "modoV", (st, v) => {
    // al pasar a manual se arranca desde lo que resolvio el modelo
    if (v === "manual") st.ver = st.ultimo.filas.map(f => Math.round(f.vertimiento_p50 / CMS_A_HM3 * 1000) / 1000);
  });
  segmento("vista", "vista");
  document.getElementById("aplicar").addEventListener("click", () => {
    const st = ESTADO[actual], k = document.getElementById("cual").value;
    let v = parseFloat(document.getElementById("valorTodos").value);
    if (!Number.isFinite(v) || v < 0) v = 0;
    if (k === "ver") { if (st.modoV !== "manual") { st.modoV = "manual"; } st.ver = new Array(N).fill(v); }
    else { if (st.modoED !== "manual") { st.modoED = "manual"; st.ent = BASE.ent.slice(); st.des = BASE.des.slice(); } st[k] = new Array(N).fill(v); }
    hayCambios = true;
    dibujarTodo();
  });
  document.getElementById("restablecer").addEventListener("click", () => {
    const st = ESTADO[actual];
    Object.assign(st, { modoED: "auto", modoV: "auto", ent: BASE.ent.slice(), des: BASE.des.slice(), ver: BASE.ver.map(x => Number.isFinite(x) ? x : 0) });
    dibujarTodo();
  });
  document.getElementById("descargar").addEventListener("click", () => {
    const arch = [];
    for (const v of D.versiones) {
      const u = calcular(v.clave);
      arch.push(...archivosVersion(D, v.clave, u.R, u.filas));
    }
    calcular(actual);
    const blob = new Blob([hacerZip(arch)], { type: "application/zip" });
    const a = document.createElement("a");
    a.href = URL.createObjectURL(blob);
    a.download = D.embalse + "_pronostico_" + D.sello + ".zip";
    document.body.appendChild(a); a.click(); a.remove();
    setTimeout(() => URL.revokeObjectURL(a.href), 2000);
    hayCambios = false;
    const h = new Date();
    document.getElementById("estadoDescarga").textContent = "Descargado a las " + String(h.getHours()).padStart(2, "0") + ":" + String(h.getMinutes()).padStart(2, "0") + ".";
  });
  // los cambios viven solo en esta pagina: avisar antes de cerrarla sin descargar
  window.addEventListener("beforeunload", e => { if (hayCambios) { e.preventDefault(); e.returnValue = ""; } });

  encabezado();
  armarTabla();
  dibujarTodo();
}
