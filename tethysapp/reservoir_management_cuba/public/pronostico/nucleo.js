// nucleo.js - calculo del balance para la pagina de pronostico.
// Es la traduccion de propagar() y tabla_balance() de pronosticar.py (y del
// Colab). Si se cambia algo aqui, hay que cambiarlo alla tambien.
"use strict";

const CMS_A_HM3 = 0.0864;

// np.interp: x creciente, se recorta a los extremos; NaN entra, NaN sale.
function interp(x, xp, fp, izq) {
  if (Number.isNaN(x)) return NaN;
  const n = xp.length;
  if (x <= xp[0]) return (izq !== undefined && x < xp[0]) ? izq : fp[0];
  if (x >= xp[n - 1]) return fp[n - 1];
  let lo = 0, hi = n - 1;
  while (hi - lo > 1) {
    const m = (lo + hi) >> 1;
    if (xp[m] <= x) lo = m; else hi = m;
  }
  const dx = xp[hi] - xp[lo];
  if (dx === 0) return fp[hi];
  return fp[lo] + (x - xp[lo]) * (fp[hi] - fp[lo]) / dx;
}

function crearEmbalse(D) {
  const c = D.curva, vt = D.vertedero;
  const E = {
    vMin: D.v_min, vMax: D.v_max, vert: vt,
    nivelDeVol: v => interp(v, c.volumen, c.nivel),
    areaDeVol: v => interp(v, c.volumen, c.area),
  };
  E.gasto = carga => {
    if (vt.tabla) return interp(vt.cresta + carga, vt.tabla.nivel, vt.tabla.gasto, 0.0);
    return vt.a * Math.pow(carga, vt.b);
  };
  return E;
}

const nan = x => (x === null || x === undefined) ? NaN : x;

// Balance diario miembro a miembro. Q[dia][miembro] en m3/s (null = sin dato).
// entrega y desembalse en hm3/dia; vertimiento en m3/s, NaN = lo resuelve el aliviadero.
function propagar(E, Q, V0, entrega, desembalse, vertimiento, evapMm, pasos) {
  pasos = pasos || 24; evapMm = evapMm || 0;
  const nd = Q.length, nm = Q[0].length;
  const oV = [], oE = [], oD = [], oS = [];
  const V = new Array(nm).fill(V0);
  for (let d = 0; d < nd; d++) {
    const rV = new Array(nm), rE = new Array(nm), rD = new Array(nm), rS = new Array(nm);
    const vd = vertimiento[d];
    const impuesto = Number.isFinite(vd);
    for (let m = 0; m < nm; m++) {
      let v = V[m];
      const qin = nan(Q[d][m]) * CMS_A_HM3;
      const ev = evapMm ? evapMm * E.areaDeVol(v) / 1000.0 : 0.0;
      let ent, des, vert;
      if (impuesto) {
        vert = vd * CMS_A_HM3;
        vert = Math.min(vert, Math.max(0.0, v + qin - ev - E.vMin));
        const disp = Math.max(0.0, v + qin - ev - vert - E.vMin);
        ent = Math.min(entrega[d], disp);
        des = Math.min(desembalse[d], disp - ent);
        v = v + qin - ev - ent - des - vert;
        const extra = Math.max(0.0, v - E.vMax);
        v = v - extra; vert = vert + extra;
      } else if (!E.vert) {
        const disp = Math.max(0.0, v + qin - ev - E.vMin);
        ent = Math.min(entrega[d], disp);
        des = Math.min(desembalse[d], disp - ent);
        const vt = v + qin - ev - ent - des;
        vert = Math.max(0.0, vt - E.vMax);
        v = vt - vert;
      } else {
        const disp = Math.max(0.0, v + qin - ev - E.vMin);
        ent = Math.min(entrega[d], disp);
        des = Math.min(desembalse[d], disp - ent);
        const neto = (qin - ev - ent - des) / pasos;
        vert = 0.0;
        for (let k = 0; k < pasos; k++) {
          const carga = Math.max(E.nivelDeVol(v) - E.vert.cresta, 0.0);
          let s = E.gasto(carga) * CMS_A_HM3 / pasos;
          s = Math.min(s, Math.max(v + neto - E.vert.v_cresta, 0.0));
          v = v + neto - s; vert = vert + s;
        }
        const extra = Math.max(0.0, v - E.vMax);
        v = v - extra; vert = vert + extra;
      }
      V[m] = v; rV[m] = v; rE[m] = ent; rD[m] = des; rS[m] = vert;
    }
    oV.push(rV); oE.push(rE); oD.push(rD); oS.push(rS);
  }
  const oN = oV.map(f => f.map(v => E.nivelDeVol(v)));
  return { V: oV, N: oN, EN: oE, DE: oD, VE: oS };
}

// np.nanpercentile con interpolacion lineal (la de numpy por defecto)
function nanpct(a, q) {
  const s = a.filter(Number.isFinite).sort((x, y) => x - y);
  if (!s.length) return NaN;
  const i = (s.length - 1) * q / 100, lo = Math.floor(i), hi = Math.ceil(i);
  return s[lo] + (s[hi] - s[lo]) * (i - lo);
}
const nanmax = a => { const s = a.filter(Number.isFinite); return s.length ? Math.max(...s) : NaN; };

function tablaBalance(fechas, Q, R, entPedida, desPedida) {
  const filas = [];
  for (let d = 0; d < fechas.length; d++) {
    const N = R.N[d], V = R.V[d];
    const vivo = N.map(Number.isFinite);
    const enm = a => a.map((x, m) => vivo[m] ? x : NaN);
    const f = { fecha: fechas[d] };
    for (const q of [10, 25, 50, 75, 90]) f["nivel_p" + String(q).padStart(2, "0")] = nanpct(N, q);
    for (const q of [10, 25, 50, 75, 90]) f["volumen_p" + String(q).padStart(2, "0")] = nanpct(V, q);
    f.entrega_pedida = entPedida[d];
    f.entrega_p50 = nanpct(enm(R.EN[d]), 50);
    f.desembalse_pedido = desPedida[d];
    f.desembalse_p50 = nanpct(enm(R.DE[d]), 50);
    f.vertimiento_p50 = nanpct(enm(R.VE[d]), 50);
    f.vertimiento_max = nanmax(enm(R.VE[d]));
    f.salida_total_p50 = f.entrega_p50 + f.desembalse_p50 + f.vertimiento_p50;
    f.Qin_p50_cms = nanpct(Q[d].map(nan), 50);
    f.miembros_que_vierten = R.VE[d].filter(x => x > 0).length;
    f.miembros_vivos = vivo.filter(Boolean).length;
    filas.push(f);
  }
  return filas;
}

// ------------------------------------------------------------- CSV como pandas
// pandas: .round(4) (redondeo al par) y luego repr del float.
function round4(x) {
  const y = x * 1e4, r = Math.round(y);
  const par = (Math.abs(y - Math.trunc(y)) === 0.5) ? 2 * Math.round(y / 2) : r;
  return par / 1e4;
}
function fmtFloat(x) {
  if (x === null || x === undefined || Number.isNaN(x)) return "";
  if (x === Infinity) return "inf";
  if (x === -Infinity) return "-inf";
  const r = round4(x);
  if (Object.is(r, -0)) return "-0.0";
  if (Number.isInteger(r) && Math.abs(r) < 1e16) return r.toFixed(1);
  let s = String(r);
  if (/e/.test(s) || Math.abs(r) < 1e-4) {            // repr de Python: 1e-05
    s = r.toExponential().replace(/e([+-])(\d)$/, "e$10$2");
  }
  return s;
}
const COLS_BAL = ["fecha", "nivel_p10", "nivel_p25", "nivel_p50", "nivel_p75", "nivel_p90",
  "volumen_p10", "volumen_p25", "volumen_p50", "volumen_p75", "volumen_p90",
  "entrega_pedida", "entrega_p50", "desembalse_pedido", "desembalse_p50",
  "vertimiento_p50", "vertimiento_max", "salida_total_p50", "Qin_p50_cms",
  "miembros_que_vierten", "miembros_vivos"];
const COLS_INT = new Set(["miembros_que_vierten", "miembros_vivos"]);

function csvBalance(filas) {
  const out = [COLS_BAL.join(",")];
  for (const f of filas)
    out.push(COLS_BAL.map(c => c === "fecha" ? f.fecha : COLS_INT.has(c) ? String(f[c]) : fmtFloat(f[c])).join(","));
  return out.join("\n") + "\n";
}
function csvMatriz(fechas, miembros, M) {
  const out = [["fecha"].concat(miembros).join(",")];
  for (let d = 0; d < fechas.length; d++) out.push([fechas[d]].concat(M[d].map(x => fmtFloat(nan(x)))).join(","));
  return out.join("\n") + "\n";
}

// ------------------------------------------------------------- ZIP sin compresion
const CRC_T = (() => { const t = new Uint32Array(256);
  for (let n = 0; n < 256; n++) { let c = n; for (let k = 0; k < 8; k++) c = (c & 1) ? 0xEDB88320 ^ (c >>> 1) : c >>> 1; t[n] = c >>> 0; }
  return t; })();
function crc32(b) { let c = 0xFFFFFFFF; for (let i = 0; i < b.length; i++) c = CRC_T[(c ^ b[i]) & 0xFF] ^ (c >>> 8); return (c ^ 0xFFFFFFFF) >>> 0; }

// archivos: [{nombre, texto}] -> Uint8Array con el .zip
function hacerZip(archivos, fecha) {
  const enc = new TextEncoder();
  fecha = fecha || new Date();
  const dosT = (fecha.getHours() << 11) | (fecha.getMinutes() << 5) | (fecha.getSeconds() >> 1);
  const dosD = ((fecha.getFullYear() - 1980) << 9) | ((fecha.getMonth() + 1) << 5) | fecha.getDate();
  const partes = [], central = [];
  let off = 0;
  for (const a of archivos) {
    const nom = enc.encode(a.nombre), dat = enc.encode(a.texto), crc = crc32(dat);
    const h = new DataView(new ArrayBuffer(30));
    h.setUint32(0, 0x04034b50, true); h.setUint16(4, 20, true); h.setUint16(6, 0x0800, true);
    h.setUint16(8, 0, true); h.setUint16(10, dosT, true); h.setUint16(12, dosD, true);
    h.setUint32(14, crc, true); h.setUint32(18, dat.length, true); h.setUint32(22, dat.length, true);
    h.setUint16(26, nom.length, true); h.setUint16(28, 0, true);
    partes.push(new Uint8Array(h.buffer), nom, dat);
    const c = new DataView(new ArrayBuffer(46));
    c.setUint32(0, 0x02014b50, true); c.setUint16(4, 20, true); c.setUint16(6, 20, true); c.setUint16(8, 0x0800, true);
    c.setUint16(10, 0, true); c.setUint16(12, dosT, true); c.setUint16(14, dosD, true);
    c.setUint32(16, crc, true); c.setUint32(20, dat.length, true); c.setUint32(24, dat.length, true);
    c.setUint16(28, nom.length, true); c.setUint32(42, off, true);
    central.push(new Uint8Array(c.buffer), nom);
    off += 30 + nom.length + dat.length;
  }
  let tamC = 0; for (const p of central) tamC += p.length;
  const f = new DataView(new ArrayBuffer(22));
  f.setUint32(0, 0x06054b50, true); f.setUint16(8, archivos.length, true); f.setUint16(10, archivos.length, true);
  f.setUint32(12, tamC, true); f.setUint32(16, off, true);
  const todo = partes.concat(central, [new Uint8Array(f.buffer)]);
  let n = 0; for (const p of todo) n += p.length;
  const out = new Uint8Array(n); let i = 0;
  for (const p of todo) { out.set(p, i); i += p.length; }
  return out;
}

// Los cuatro CSV de una version, con los mismos nombres que el Colab
function archivosVersion(D, clave, R, filas) {
  const pre = clave + "/" + D.embalse + "_";
  return [
    { nombre: pre + "pronostico_" + D.sello + ".csv", texto: csvMatriz(D.fechas, D.miembros, D.Q[clave]) },
    { nombre: pre + "balance_" + D.sello + ".csv", texto: csvBalance(filas) },
    { nombre: pre + "balance_" + D.sello + "_miembros.csv", texto: csvMatriz(D.fechas, D.miembros, R.N) },
    { nombre: pre + "balance_" + D.sello + "_miembros_volumen.csv", texto: csvMatriz(D.fechas, D.miembros, R.V) },
  ];
}

if (typeof module !== "undefined") {
  module.exports = { interp, crearEmbalse, propagar, tablaBalance, csvBalance, csvMatriz, fmtFloat,
    hacerZip, archivosVersion, nanpct, CMS_A_HM3 };
}
