// Entry point: wire the DOM to the handler modules and lock the page until a
// wallet connects. No app logic here, only wiring.
import { $ } from "./dom.js";
import { connectWallet } from "./wallet.js";
import { bobNew, aliceDerive, alicePay, bobCollect, loadConfig } from "./flow.js";
import { vaultCreate, vaultImport, vaultWithdraw, tryPlainWithdraw, usePasskeyExisting } from "./vault.js";
import { decodeTx, fillTx } from "./decoder.js";

// [element id, handler] for plain click targets.
const clicks = [
  ["c-btn",      connectWallet],
  ["b-new",      bobNew],
  ["b-vault",    vaultCreate],
  ["b-import",   vaultImport],
  ["b-collect",  bobCollect],
  ["a-derive",   aliceDerive],
  ["a-pay",      alicePay],
  ["v-withdraw", vaultWithdraw],
  ["v-attack",   tryPlainWithdraw],
  ["d-go",       decodeTx],
];
for(const [id, fn] of clicks){
  const el = $(id); if(el) el.addEventListener("click", fn);
}

// Link targets that must not follow the href.
const links = [
  ["use-existing", usePasskeyExisting],
  ["q-fund",       () => fillTx("fund")],
  ["q-collect",    () => fillTx("collect")],
];
for(const [id, fn] of links){
  const el = $(id);
  if(el) el.addEventListener("click", e => { e.preventDefault(); fn(); });
}

document.body.classList.add("locked");
loadConfig();   // hides "3 · Pay" when the server has no funder key (external-payment mode)
