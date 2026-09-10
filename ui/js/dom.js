// Small DOM helpers shared across the UI. No app logic lives here.
export const $ = id => document.getElementById(id);
export const short = a => a ? a.slice(0, 8) + "…" + a.slice(-6) : "";

// Main status line at the bottom of the page.
export function log(msg, cls){
  const o = $("out"); o.className = "out" + (cls ? " " + cls : ""); o.textContent = msg;
}

// Toggle a button into a spinner while an async action runs.
export function busy(btn, on){
  btn.dataset.t = btn.dataset.t || btn.textContent;
  btn.disabled = on;
  btn.innerHTML = on ? '<span class="spin"></span> working…' : btn.dataset.t;
}

// Status line inside the PQ smart wallet card.
export function vlog(msg, cls){
  const o = $("v-out"); o.className = "vout";
  o.innerHTML = cls ? `<span class="${cls}">${msg}</span>` : msg;
}

// Drive the post-quantum phase banner (wait / pq / classical).
export function setPhase(kind, title, note){
  $("pq-phase").hidden = false;
  $("pq-phase").className = "pqphase " + kind;
  $("pq-title").textContent = title;
  $("pq-note").textContent = note;
}
