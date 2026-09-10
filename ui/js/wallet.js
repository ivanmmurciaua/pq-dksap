// Wallet connection on the correct network. The connected EOA pays gas for the
// PQ smart wallet (deploy, withdraw); the backend never touches its funds.
import { $, short } from "./dom.js";
import { state } from "./state.js";

const HEX_CHAIN = "0x1fcd"; // 8141

function walletErr(msg){
  $("connect").querySelector("div").innerHTML = `<span style="color:var(--bad)">${msg}</span>`;
}

export async function connectWallet(){
  if(location.protocol !== "http:" && location.protocol !== "https:"){
    walletErr("Open the app at http://localhost:8000 (the server), not a file:// path. Wallets need a real page origin, and a file:// origin is what makes MetaMask throw \"reading 'origin'\"."); return;
  }
  if(!window.ethereum){ walletErr("No wallet found. Install MetaMask, then reload."); return; }
  const btn = $("c-btn"); btn.disabled = true; btn.textContent = "connecting…";
  const reset = () => { btn.disabled = false; btn.textContent = "Connect wallet"; };
  try{
    const accts = await window.ethereum.request({method:"eth_requestAccounts"});
    let chain = await window.ethereum.request({method:"eth_chainId"});
    if(chain !== HEX_CHAIN){
      try{ await window.ethereum.request({method:"wallet_switchEthereumChain", params:[{chainId:HEX_CHAIN}]}); }
      catch(e){
        if(e && (e.code === 4902 || (e.data && e.data.originalError && e.data.originalError.code === 4902))){
          await window.ethereum.request({method:"wallet_addEthereumChain", params:[{
            chainId:HEX_CHAIN, chainName:"ethrex Hegota privacy testnet",
            rpcUrls:["https://rpc1.privacy.ethrex.xyz"],
            blockExplorerUrls:["https://dora.privacy.ethrex.xyz"],
            nativeCurrency:{name:"Ether", symbol:"ETH", decimals:18}}]});
        } else { walletErr("Add/switch to the Hegota network (chain 8141) manually in MetaMask, then retry. ("+(e.message||e)+")"); return reset(); }
      }
      chain = await window.ethereum.request({method:"eth_chainId"});
      if(chain !== HEX_CHAIN){ walletErr("Still on the wrong network. Switch to Hegota (8141) and retry."); return reset(); }
    }
    state.wallet = accts[0];
    window.ethereum.on("chainChanged", ()=>location.reload());
    window.ethereum.on("accountsChanged", ()=>location.reload());
    document.body.classList.remove("locked");
    $("connect").hidden = true; $("wallet-bar").hidden = false;
    $("w-acct").textContent = short(state.wallet);
  }catch(e){ walletErr("Connection failed: "+(e.message||e)); reset(); }
}

// Send a tx from the connected wallet (it pays gas and signs).
export async function walletSend(to, data){
  return await window.ethereum.request({method:"eth_sendTransaction", params:[{from:state.wallet, to, data}]});
}

// Poll for a receipt (the wallet's own RPC), up to ~5 minutes.
export async function waitReceipt(h){
  for(let i=0;i<100;i++){
    const r = await window.ethereum.request({method:"eth_getTransactionReceipt", params:[h]});
    if(r) return r;
    await new Promise(s=>setTimeout(s,3000));
  }
  return null;
}
