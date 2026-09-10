// The stealth-payment flow: recipient identity, payer derive + pay, and the
// self-paid deploy+sweep collect. Plus the deposit listener and config load.
import { $, log, busy, short, setPhase } from "./dom.js";
import { state } from "./state.js";
import { api } from "./api.js";
import { vaultRefresh } from "./vault.js";

export function stopDepositWatch(){
  if(state.depositTimer){ clearInterval(state.depositTimer); state.depositTimer=null; }
}
export function startDepositWatch(addr){
  stopDepositWatch();
  $("deposit").hidden = false;
  const tick = async () => {
    try{ const b = await api("/api/balance",{addr});
      if(b.funded){
        $("deposit").className = "deposit on";
        $("deposit").innerHTML = b.ready
          ? `deposit detected: <b>${b.eth} ETH</b>, ready to sweep (step 4)`
          : `deposit detected: <b>${b.eth} ETH</b>, below the ~${b.reserve_eth} ETH needed for the self-paid sweep gas; send a little more`;
        setPhase("pq","Funds are quantum-safe",
          "They sit at the stealth account: no secp256k1 key controls this address, only an ML-KEM-protected ML-DSA commit can move them, so a quantum attacker cannot.");
      } else {
        $("deposit").className = "deposit";
        $("deposit").innerHTML = "watching the stealth address for a deposit…";
      }
    }catch(e){ /* transient; keep polling */ }
  };
  tick();
  state.depositTimer = setInterval(tick, 4000);
}

// Read the backend config and toggle payer-only UI. The top bar chips are
// optional (may be commented out in the markup), so every bar write is guarded;
// the one thing that MUST hold is hiding "3 · Pay" when the server has no funder
// key (external-payment mode: the payer sends from their own wallet via the QR).
export async function loadConfig(){
  const set = (id, v) => { const el = $(id); if(el) el.textContent = v; };
  const hide = id => { const el = $(id); if(el) el.hidden = true; };
  try{ const c = await api("/api/config");
    state.gasless = !!c.gasless;   // vault deploy/withdraw paid by the sponsor
    set("c-chain", c.chain_id);
    set("c-fac", c.factory ? short(c.factory) : "NONE, run factory-deploy");
    if(c.pay_button){
      set("c-alice", short(c.alice));
      set("c-bal", c.alice_balance);
    } else {
      hide("chip-alice"); hide("chip-bal");
      hide("a-pay");     // no funder key: the payer pays from their own wallet
      hide("q-fund");    // and no in-app funding tx exists to decode
    }
  }catch(e){ log("config error: "+e.message,"bad"); }
}

export async function bobNew(){
  stopDepositWatch();
  $("pq-phase").hidden = true; $("deposit").hidden = true; $("banner").style.display = "none";
  $("vault-card").hidden = true; $("v-out").innerHTML = ""; state.vault = null;
  const btn=$("b-new"); busy(btn,true); log("The recipient generates an ML-DSA master key + an ML-KEM viewing key…");
  try{ const r = await api("/api/bob/new",{});
    state.handle = r.handle;
    $("b-handle").textContent = r.handle;
    $("b-meta").textContent = r.meta_bytes;
    $("b-info").hidden = false;
    $("b-vault").disabled = false;
    $("b-import").disabled = false;
    log("The recipient's meta-address is ready ("+r.meta_bytes+" B). Create your PQ smart wallet next (1b) to keep the payout post-quantum. -> step 1b","ok");
  }catch(e){ log("error: "+e.message,"bad"); } finally{ busy(btn,false); }
}

export async function aliceDerive(){
  const btn=$("a-derive"); busy(btn,true); log("The payer runs blinded ML-DSA over an ML-KEM shared secret…");
  try{ const r = await api("/api/alice/derive",{handle:state.handle});
    state.address = r.stealth_address;
    $("a-addr").textContent = r.stealth_address;
    $("a-code").textContent = "CREATE2 address, awaiting its deploy ("+r.code+" B) · commit 0x"+r.commit.slice(0,12)+"…";
    $("a-info").hidden = false;
    $("qr").style.display="block"; $("qr").innerHTML="";
    new window.QRCode($("qr"),{text:r.stealth_address,width:150,height:150,colorDark:"#0b0d12",colorLight:"#ffffff"});
    $("qr-hint").style.display="block";
    $("a-pay").disabled=false;
    $("b-collect").disabled=false;
    setPhase("wait","Address ready, awaiting deposit",
      "Nothing is at risk yet: the address holds no funds. Post-quantum custody begins the moment ETH lands here.");
    startDepositWatch(state.address);
    const payHint = $("a-pay").hidden
      ? "Send ETH to it from any wallet (scan the QR), then collect (step 4)."
      : "Pay with step 3, or send ETH to it from any wallet, then collect (step 4).";
    log("The recipient's one-time address is ready. "+payHint,"ok");
  }catch(e){ log("error: "+e.message,"bad"); } finally{ busy(btn,false); }
}

export async function alicePay(){
  const btn=$("a-pay"); busy(btn,true); log("The payer sends a plain transfer to the address, then walks away…");
  try{ const r = await api("/api/alice/pay",{stealth_address:state.address});
    if(!r.ok) throw new Error("funding reverted");
    state.fundTx = r.tx;
    $("b-collect").disabled=false;
    log("Funded: the stealth address holds "+r.held+" ETH (block "+r.block+"). The recipient can now collect. -> step 4","ok");
  }catch(e){ log("error: "+e.message,"bad"); } finally{ busy(btn,false); }
}

export async function bobCollect(){
  const btn=$("b-collect"); busy(btn,true);
  log("The recipient recovers the blinded key and sends ONE 0x06 tx that deploys the account AND sweeps.\nVerifying ML-DSA onchain (~5.7M gas)… this waits for a block.");
  try{ const r = await api("/api/bob/collect",{stealth_address:state.address});
    state.collectTx = r.tx;
    if(!r.ok) throw new Error("collect reverted (tx "+r.tx+")");
    stopDepositWatch();
    $("deposit").hidden = true;
    if(r.to_vault){
      setPhase("pq","Funds are in your PQ smart wallet",
        "Swept into your persistent PQ smart wallet: still quantum-safe. Only your ML-DSA signature can move them out, hard-enforced onchain, the address is public and reusable, yet safe.");
      $("vault-card").hidden = false; vaultRefresh();
      if(r.vault_deployed === false) log("Swept into the smart wallet, but its onchain deploy did not confirm. Retry the withdraw once it is deployed.","bad");
    } else {
      setPhase("classical","Swept to a classical payout",
        "The funds now sit in a secp256k1 wallet, back to classical security: whoever holds that k1 key (or a quantum computer that breaks it) controls them. To stay post-quantum end to end, the payout must itself be a PQ account.");
    }
    $("banner").style.display="block";
    $("win-bal").textContent = r.bob_balance;
    $("win-gas").textContent = r.gas.toLocaleString();
    $("win-tx").textContent = short(r.tx);
    log("Done. The account deployed ("+r.code+" B) and swept in one self-paid tx, covered by the stealth account itself.","ok");
    const cbal = $("c-bal"); if(cbal) cbal.textContent = "-";   // top bar is optional
  }catch(e){ log("error: "+e.message,"bad"); } finally{ busy(btn,false); }
}
