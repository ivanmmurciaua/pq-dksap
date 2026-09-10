// WebAuthn PRF: the post-quantum key source for the PQ smart wallet. The PRF
// secret lives in the authenticator's secure element and is not derivable from
// any public key, so it does not inherit the k1 wallet's quantum weakness.

// The anchor string seeds every PQ smart-wallet key. NEVER change it: doing so
// re-derives every wallet at a new address and orphans existing funds.
export const VAULT_ANCHOR = "pq-dksap PQ vault key v1";

export function bytesToHex(b){
  return "0x" + [...new Uint8Array(b)].map(x=>x.toString(16).padStart(2,"0")).join("");
}
export async function sha256Bytes(str){
  return new Uint8Array(await crypto.subtle.digest("SHA-256", new TextEncoder().encode(str)));
}

// localStorage pointers: which key source this browser uses, and the exact
// passkey credential id, so the SAME passkey is reused every time (a different
// passkey would derive a different smart wallet).
export const PK_METHOD_KEY = "pq-dksap-vault-method";   // "prf" | "wallet" once known
const PK_CRED_KEY = "pq-dksap-vault-cred";              // base64url credential id
export function lsGet(k){ try{ return localStorage.getItem(k); }catch(e){ return null; } }
export function lsSet(k,v){ try{ localStorage.setItem(k,v); }catch(e){} }

function b64uFromBytes(buf){
  let s = ""; const b = new Uint8Array(buf);
  for(let i=0;i<b.length;i++) s += String.fromCharCode(b[i]);
  return btoa(s).replace(/\+/g,"-").replace(/\//g,"_").replace(/=+$/,"");
}
function bytesFromB64u(str){
  str = str.replace(/-/g,"+").replace(/_/g,"/"); while(str.length%4) str += "=";
  const s = atob(str); const b = new Uint8Array(s.length);
  for(let i=0;i<s.length;i++) b[i] = s.charCodeAt(i); return b;
}

// Get the WebAuthn PRF secret bound to `salt`, as 0x-hex, or null if no
// authenticator here supports PRF (caller then uses k1 + PIN). Deterministic:
// - a stored credential id  -> go straight to that passkey (no picker)
// - opts.useExisting         -> show the picker so a synced passkey can be chosen
// - otherwise (first time)   -> create one discoverable passkey and remember it
export async function passkeyPrf(salt, opts){
  opts = opts || {};
  if(!window.PublicKeyCredential || !navigator.credentials) return null;
  const rpId = location.hostname;
  const readPrf = c => (c && c.getClientExtensionResults) ? c.getClientExtensionResults().prf : null;
  const evalGet = async (allowId) => {
    const pub = { challenge: crypto.getRandomValues(new Uint8Array(32)),
      rpId, userVerification:"required", timeout:60000,
      extensions:{ prf:{ eval:{ first: salt } } } };
    if(allowId) pub.allowCredentials = [{ type:"public-key", id: allowId }];
    try{ return await navigator.credentials.get({ publicKey: pub }); }catch(e){ return null; }
  };

  const storedId = lsGet(PK_CRED_KEY);
  let cred;
  if(opts.useExisting){
    cred = await evalGet(null);                       // picker: choose a (synced) passkey
    if(cred) lsSet(PK_CRED_KEY, b64uFromBytes(cred.rawId));
  } else if(storedId){
    cred = await evalGet(bytesFromB64u(storedId));    // known passkey, no picker
  } else {
    // First time on this device: create one discoverable passkey with PRF.
    let created;
    try{
      created = await navigator.credentials.create({ publicKey:{
        challenge: crypto.getRandomValues(new Uint8Array(32)),
        rp:{ id: rpId, name:"pq-dksap" },
        user:{ id: crypto.getRandomValues(new Uint8Array(16)),
               name:"pq-dksap smart wallet", displayName:"pq-dksap smart wallet" },
        pubKeyCredParams:[{type:"public-key",alg:-7},{type:"public-key",alg:-257}],
        authenticatorSelection:{ residentKey:"required", userVerification:"required" },
        timeout:60000, extensions:{ prf:{ eval:{ first: salt } } } } });
    }catch(e){ return null; }
    const ext = readPrf(created);
    if(!ext || ext.enabled === false) return null;    // no PRF here -> caller uses k1 + PIN
    lsSet(PK_CRED_KEY, b64uFromBytes(created.rawId));
    if(ext.results && ext.results.first) return bytesToHex(new Uint8Array(ext.results.first));
    cred = await evalGet(created.rawId);              // read the PRF via a follow-up get
  }
  const prf = readPrf(cred);
  if(!prf || !prf.results || !prf.results.first) return null;
  return bytesToHex(new Uint8Array(prf.results.first));
}
