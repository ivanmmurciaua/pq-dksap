// Transaction decoder: fetch a tx by hash and render it frame by frame.
import { $, log } from "./dom.js";
import { state } from "./state.js";
import { api } from "./api.js";

// Fill the decoder input from a tx produced during this run.
export function fillTx(which){
  const h = which==="fund" ? state.fundTx : state.collectTx;
  if(!h){ log("no "+which+" tx yet, run the flow first","bad"); return; }
  $("d-hash").value = h; decodeTx();
}

function frameRow(f){
  const money = f.value_eth>0 ? ` · <span class="ok">${f.value_eth.toFixed(6)} ETH</span>` : "";
  const to = f.to ? f.to : "self (the account)";
  return `<div class="frame">
    <div class="head"><span class="fidx">#${f.index}</span>
      <span class="badge b-${f.role}">${f.mode} ${f.mode_hex||""}</span>
      <span style="color:var(--dim);font-size:12px">-> ${to}${money}</span></div>
    <div class="fmeta">exec gas ${f.exec_gas.toLocaleString()} · state gas ${f.state_gas.toLocaleString()}${f.data_bytes?` · data ${f.data_bytes} B`:""}${f.scope!=="none"?` · ${f.scope}`:""}</div>
    <div class="fnote">${f.note}</div></div>`;
}

export async function decodeTx(){
  const h=$("d-hash").value.trim(); if(!h) return;
  const box=$("d-out"); box.innerHTML='<div class="dsummary"><span class="spin"></span> fetching…</div>';
  try{ const d = await api("/api/decode",{tx:h});
    if(!d.found){ box.innerHTML='<div class="dsummary bad">transaction not found</div>'; return; }
    if(d.kind==="legacy"){
      box.innerHTML=`<div class="dsummary">${d.summary}</div>
        <div class="frame"><div class="head"><span class="badge">LEGACY ${d.type}</span>
        <span style="color:var(--dim);font-size:12px">${d.from} -> ${d.to} · <span class="ok">${d.value_eth.toFixed(6)} ETH</span></span></div>
        <div class="fnote">A classic ECDSA-signed transfer that pre-funds the stealth address.</div></div>`;
      return;
    }
    let html=`<div class="dsummary">${d.summary}</div>
      <div class="fmeta">sender ${d.sender} · nonce ${d.nonce} · prefix: <b style="color:var(--ink)">${d.prefix_shape}</b></div>`;
    html += d.frames.map(frameRow).join("");
    d.signatures.forEach(s=>{ html+=`<div class="frame"><div class="head">
      <span class="badge">SIG ${s.scheme}</span>
      <span style="color:var(--dim);font-size:12px">${s.signature_bytes.toLocaleString()} B inline</span></div>
      ${s.note?`<div class="fnote">${s.note}</div>`:""}</div>`; });
    box.innerHTML=html;
  }catch(e){ box.innerHTML='<div class="dsummary bad">error: '+e.message+'</div>'; }
}
