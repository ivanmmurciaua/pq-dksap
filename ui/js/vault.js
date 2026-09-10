// The PQ smart wallet: derive its ML-DSA key source, create/import it, and
// withdraw (ML-DSA signed) or demonstrate a plain k1 withdraw that reverts.
import { $, short, vlog, log, busy, setPhase } from "./dom.js";
import { state } from "./state.js";
import { api } from "./api.js";
import { walletSend, waitReceipt } from "./wallet.js";
import { passkeyPrf, sha256Bytes, VAULT_ANCHOR, PK_METHOD_KEY, lsGet, lsSet } from "./passkey.js";

export async function vaultRefresh(){
  if(!state.handle) return;
  try{ const s = await api("/api/vault/status",{handle:state.handle});
    if(!s.vault) return;
    $("v-addr").textContent = s.vault;
    $("v-dep").textContent = s.deployed ? "(deployed)" : "(counterfactual)";
    $("v-bal").textContent = s.eth;
  }catch(e){}
}

// Inline PIN prompt (no blocking browser dialog). Resolves with the typed value.
function askPin(){
  return new Promise(resolve=>{
    const row=$("pin-row"), input=$("v-pin"), ok=$("v-pin-ok");
    row.hidden=false; input.value=""; row.scrollIntoView({block:"center"}); input.focus();
    const done=v=>{ row.hidden=true; ok.onclick=null; input.onkeydown=null; resolve(v); };
    ok.onclick=()=>done(input.value.trim());
    input.onkeydown=e=>{ if(e.key==="Enter") done(input.value.trim()); };
  });
}

// Resolve the key source: passkey PRF (post-quantum) if the authenticator
// supports it, else the wallet signature + a user PIN. The choice is remembered
// in localStorage so we do not re-probe on every operation.
export async function getVaultAuth(opts){
  opts = opts || {};
  if(state.vaultAuth && !opts.useExisting) return state.vaultAuth;
  const remembered = lsGet(PK_METHOD_KEY);
  // Try the passkey path when forced (useExisting), when remembered as prf, or
  // on the very first use (no remembered method). A remembered "wallet" skips it.
  if(opts.useExisting || remembered === "prf" || !remembered){
    vlog(opts.useExisting ? "Pick your existing vault passkey…"
         : "Checking for your vault passkey (post-quantum key source)…");
    let secret = null;
    try{ secret = await passkeyPrf(await sha256Bytes(VAULT_ANCHOR), opts); }catch(e){ secret = null; }
    if(secret){
      lsSet(PK_METHOD_KEY, "prf");
      state.vaultAuth = { method:"prf", secret };
      vlog("Smart-wallet key derived from a passkey PRF secret. It lives in your authenticator's secure element and never depends on your k1 wallet, so a stolen or quantum-broken k1 key cannot reach these funds.","ok");
      return state.vaultAuth;
    }
    if(opts.useExisting) throw new Error("no passkey selected, or it has no PRF support");
    if(!remembered) lsSet(PK_METHOD_KEY, "wallet");   // this device has no PRF; do not re-probe
  }
  vlog("Using the k1 fallback: sign to derive, protected by a PIN (choose a strong one; it protects the key if your k1 is ever stolen)…");
  const pin = await askPin();
  if(!pin || pin.length < 6) throw new Error("a PIN or passphrase of 6+ characters is required for the k1 fallback");
  const sig = await window.ethereum.request({method:"personal_sign", params:[VAULT_ANCHOR, state.wallet]});
  state.vaultAuth = { method:"wallet", sig, pin };
  return state.vaultAuth;
}

// Escape hatch: on a new device (or after clearing site data) pick the synced
// passkey so smart wallets re-derive to the same addresses, before create/import.
export async function usePasskeyExisting(){
  try{
    await getVaultAuth({useExisting:true});
    vlog("Existing passkey selected. Now create (1b) or import your PQ smart wallet, it will re-derive to the same addresses.","ok");
  }catch(e){ vlog("error: "+(e.message||e),"bad"); }
}

export async function vaultCreate(){
  const btn=$("b-vault"); busy(btn,true); $("vault-card").hidden=false;
  try{
    const auth = await getVaultAuth();
    state.vault = null; $("v-dep").textContent = "";
    const r = await api("/api/vault/create",{handle:state.handle, auth});
    state.vault = r.vault; $("v-addr").textContent = r.vault;
    if(r.gasless){
      // COUNTERFACTUAL: only the address is derived here; the sponsor deploys the
      // vault at COLLECT, when the sweep lands in it.
      vlog("PQ smart wallet #"+r.index+" ready at "+short(r.vault)+" (counterfactual). It is not deployed yet, it deploys when you collect and the sweep lands in it.","ok");
    } else {
      // Wallet path: no sponsor, so the connected wallet deploys it now.
      vlog("Deploying smart wallet #"+r.index+", confirm in your wallet (you pay the gas)…");
      const h = await walletSend(r.factory, r.deploy_calldata);
      await waitReceipt(h);
      vlog("PQ smart wallet #"+r.index+" deployed at "+short(r.vault)+". Collect sweeps into it.","ok");
    }
    await vaultRefresh(); $("a-derive").disabled = false;
    log("PQ smart wallet #"+r.index+(r.gasless?" derived (counterfactual, deploys on collect)":" deployed")+". Now the payer can derive an address. -> step 2","ok");
  }catch(e){ vlog("error: "+(e.message||e),"bad"); } finally{ busy(btn,false); }
}

export async function vaultImport(){
  const addr = $("v-import-addr").value.trim();
  if(!addr){ vlog("Paste a PQ smart wallet address to import.","bad"); return; }
  const btn=$("b-import"); busy(btn,true); $("vault-card").hidden=false;
  try{
    const auth = await getVaultAuth();
    const r = await api("/api/vault/import",{handle:state.handle, auth, address:addr});
    if(!r.owner){ vlog("That smart wallet is NOT owned by your connected wallet, so you cannot spend from it.","bad"); return; }
    state.vault = r.vault; $("v-addr").textContent = r.vault;
    $("v-dep").textContent = "smart wallet #"+r.index+(r.deployed?" (loaded)":" (yours, not deployed, use Create)");
    await vaultRefresh(); $("a-derive").disabled = false;
    vlog("Owner confirmed. Loaded your smart wallet #"+r.index+" ("+r.eth+" ETH).","ok");
    log("Loaded smart wallet #"+r.index+" (owner verified). Now the payer can derive an address. -> step 2","ok");
  }catch(e){ vlog("error: "+(e.message||e),"bad"); } finally{ busy(btn,false); }
}

export async function vaultWithdraw(){
  const btn=$("v-withdraw"); busy(btn,true);
  vlog(state.gasless
    ? "Signing the withdraw with your ML-DSA key; the sponsor submits and pays the gas…"
    : "Signing the withdraw with your ML-DSA key; confirm the submit in your wallet (~5M gas)…");
  try{ const r = await api("/api/vault/withdraw",{handle:state.handle, dest:state.wallet});
    let ok;
    if(r.gasless){
      ok = r.ok;
    } else {
      const h = await walletSend(r.to, r.calldata);
      const rc = await waitReceipt(h);
      ok = rc && rc.status==="0x1";
    }
    await vaultRefresh();
    if(ok){
      vlog(`Withdrawn to your connected wallet ${short(r.dest)}${r.gasless?", gasless (sponsor paid). tx "+short(r.tx):""}. Those funds are in a k1 wallet now, the deliberate exit to classical.`,"ok");
      setPhase("classical","Withdrawn to a classical wallet",
        "You moved funds out of the PQ smart wallet to a secp256k1 address, classical security from here. While they stayed in it they were quantum-safe.");
    } else { vlog("withdraw reverted onchain","bad"); }
  }catch(e){ vlog("error: "+(e.message||e),"bad"); } finally{ busy(btn,false); }
}

export async function tryPlainWithdraw(){
  const btn=$("v-attack"); busy(btn,true);
  vlog("Calling withdraw with the smart wallet's public key but no valid ML-DSA signature (a plain k1 call)…");
  try{ const r = await api("/api/vault/plain-withdraw",{handle:state.handle});
    let html = r.reverted
      ? `<span class="rev">REVERTED</span>: a plain k1 withdraw cannot move these funds. Even with the public key and ${r.amount_eth} ETH inside, without the ML-DSA signature it fails.\n<span class="dim">${(r.revert||"").slice(0,200)}</span>`
      : `<span class="bad">unexpected: it did not revert</span>`;
    html += `\n\nDo the same call from your own wallet? Confirm it in MetaMask; it reverts onchain (status 0x0) and you only pay the gas of a failed call:`;
    vlog(html);
    const go = document.createElement("button");
    go.textContent="Send the plain withdraw from my wallet"; go.className="danger"; go.style.marginTop="10px";
    go.onclick = async () => { go.disabled=true; go.textContent="sending…";
      const res = await walletPlainWithdraw(r.to, r.calldata);
      const d=document.createElement("div"); d.style.marginTop="8px"; d.innerHTML=res; $("v-out").appendChild(d);
      go.textContent="Send the plain withdraw from my wallet"; go.disabled=false;
    };
    $("v-out").appendChild(go);
  }catch(e){ vlog("error: "+e.message,"bad"); } finally{ busy(btn,false); }
}

async function walletPlainWithdraw(to, data){
  try{
    const h = await walletSend(to, data);
    const rc = await waitReceipt(h);
    return (rc && rc.status === "0x0")
      ? `<span class="rev">reverted onchain</span> (tx ${short(h)}, status 0x0). The smart wallet kept every wei: a plain k1 withdraw cannot touch PQ-protected funds.`
      : `sent ${short(h)}, check the receipt (it should be status 0x0).`;
  }catch(e){ return `<span class="rev">your wallet blocked it or the node rejected it</span>: ${(e.message||e).toString().slice(0,160)}`; }
}
