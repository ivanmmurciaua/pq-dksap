#!/usr/bin/env python3
"""Minimal local UI backend for the post-quantum dual-key stealth demo.

Wraps pq_dksap (blinded ML-DSA derivation + the self-paid CREATE2 deploy+sweep)
behind a tiny JSON API and serves the single-page UI. No framework, no install:
just the standard library. The heavy PQ crypto stays in Python; the page is a
thin client. Alice's key and Bob's seed live only in this process's memory.

Run:
    export PQ_FUNDER_KEY=0x<alice test-eth key>
    export PQ_FACTORY=0x<create2 factory>          # from `demo.py factory-deploy`
    python ui/server.py                            # http://localhost:8000

This is a DEMO: the backend derives Bob's seed and holds Alice's key. A
trustless build moves the crypto client-side (see the repo README roadmap).
"""
import json
import os
import secrets
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from pq_dksap import dksap, framedecode, rpc, sponsor, stealth, vault  # noqa: E402
from pq_dksap.config import CHAIN_ID, FACTORY         # noqa: E402
from eth_keys import keys                             # noqa: E402

PORT = int(os.environ.get("PQ_UI_PORT", "8000"))
HERE = os.path.dirname(__file__)
ETH = 10 ** 18

# Static assets served from the ui/ directory (the thin client).
STATIC_TYPES = {".html": "text/html; charset=utf-8", ".css": "text/css; charset=utf-8",
                ".js": "text/javascript; charset=utf-8", ".map": "application/json"}

FACTORY_ADDR = os.environ.get("PQ_FACTORY", FACTORY)
FUNDER_KEY = os.environ.get("PQ_FUNDER_KEY", "")

# Gasless sponsorship: independent of Alice. When both are set, vault
# deploy and withdraw are paid by the sponsor contract (the recipient needs zero
# ETH); the operator key only signs the authorization, it holds no funds.
# See scripts/deploy_sponsor.py to set these up once.
SPONSOR_ADDR = os.environ.get("PQ_SPONSOR", "")
OPERATOR_KEY = os.environ.get("PQ_OPERATOR_KEY", "")
GASLESS = bool(SPONSOR_ADDR and OPERATOR_KEY)

# In-memory demo state.
BOBS = {}        # handle -> {seed, meta_pub, meta_sec, vault, vault_key}
PAYMENTS = {}    # stealth_address -> {handle, kem_ct, view_tag, amount}


def alice():
    if not FUNDER_KEY:
        raise RuntimeError("set PQ_FUNDER_KEY (Alice's test-ETH key)")
    return FUNDER_KEY, keys.PrivateKey(
        bytes.fromhex(FUNDER_KEY.removeprefix("0x"))).public_key.to_checksum_address()


def eth(wei):
    return f"{wei / ETH:.6f}"


# -- API handlers: each returns a JSON-able dict ---------------------------
def api_config(_):
    a = alice()[1] if FUNDER_KEY else None
    return {"chain_id": CHAIN_ID, "factory": FACTORY_ADDR, "alice": a,
            "alice_balance": eth(rpc.get_balance(a)) if a else None,
            "pay_button": bool(FUNDER_KEY), "gasless": GASLESS}


def api_bob_new(_):
    """Bob creates a post-quantum meta-address (his stealth identity). The payout
    is his PQ vault, not a k1 wallet."""
    handle = secrets.token_hex(4)
    seed = secrets.token_bytes(32)
    meta_pub, meta_sec = dksap.gen_meta(seed)
    BOBS[handle] = {"seed": seed, "meta_pub": meta_pub, "meta_sec": meta_sec}
    return {"handle": handle, "meta_bytes": len(meta_pub.encode())}


def api_alice_derive(body):
    """Alice derives Bob's one-time stealth address from his meta-address."""
    b = BOBS.get(body["handle"])
    if not b:
        raise KeyError("unknown handle")
    if not FACTORY_ADDR:
        raise RuntimeError("no factory: run demo.py factory-deploy, set PQ_FACTORY")
    tgt = dksap.sender_derive(b["meta_pub"])
    addr = stealth.predict_stealth_address(FACTORY_ADDR, tgt.commit)
    PAYMENTS[addr.lower()] = {"handle": body["handle"], "kem_ct": tgt.kem_ct,
                              "view_tag": tgt.view_tag, "commit": tgt.commit}
    return {"stealth_address": addr, "commit": tgt.commit.hex(),
            "ct_bytes": len(tgt.kem_ct), "code": len(rpc.get_code(addr)) // 2 - 1}


def api_alice_pay(body):
    """Alice funds the predicted address with a plain transfer, then walks away."""
    addr = body["stealth_address"]
    amount = int(body.get("amount", 10 ** 14))
    allowance = int(body.get("gas_allowance", 3 * 10 ** 15))
    ah, a = alice()
    PAYMENTS[addr.lower()]["amount"] = amount
    rc = stealth.fund_address(ah, a, addr, amount + allowance)
    ok = rc and rc.get("status") == "0x1"
    return {"ok": bool(ok), "tx": rc.get("transactionHash") if rc else None,
            "block": int(rc["blockNumber"], 16) if ok else None,
            "held": eth(rpc.get_balance(addr))}


def api_bob_collect(body):
    """Bob collects: ONE self-paid 0x06 tx deploys the account AND sweeps."""
    addr = body["stealth_address"]
    p = PAYMENTS.get(addr.lower())
    b = BOBS.get(p["handle"])
    bkey = dksap.recipient_recover(b["meta_pub"], b["meta_sec"], p["kem_ct"],
                                   view_tag=p["view_tag"])
    if bkey.commit != p["commit"]:
        raise RuntimeError("recovered key mismatch")
    # Send everything the stealth holds to the payout, keeping back only the
    # reserve the account must retain to self-pay the deploy+sweep gas. Works
    # whether funded by the "Pay" button or by an external wallet.
    reserve = stealth.sweep_reserve_wei()
    balance = rpc.get_balance(addr)
    if balance <= reserve:
        raise RuntimeError(f"stealth holds {balance/ETH:.6f} ETH: fund it above {reserve/ETH:.6f}")
    amount = balance - reserve
    # The sweep always lands in the recipient's PQ vault (custody stays
    # post-quantum). The vault is required (step 1b).
    if not b.get("vault"):
        raise RuntimeError("create your PQ vault first (step 1b)")
    dest = b["vault"]
    tx = stealth.build_deploy_spend(FACTORY_ADDR, addr, p["commit"], dest, amount)
    dksap.authorize(tx, bkey)
    txh = rpc.send_raw(tx.encode_hex())
    receipt = stealth._wait(txh, timeout=240)
    ok = receipt and receipt.get("status") == "0x1"

    # The sweep has landed in the (counterfactual) vault. Now deploy the vault
    # contract there so it can be withdrawn from. Gasless: the sponsor pays it in a
    # separate frame tx. CREATE2 to an address that already holds ETH keeps the
    # balance, so the funds are untouched.
    vault_deployed = len(rpc.get_code(dest)) > 2
    vault_deploy_tx = None
    if ok and GASLESS and not vault_deployed:
        vkey = b["vault_key"]
        dcd = vkey.commit + vault.vault_init_code(vkey.commit)
        drc = sponsor.relay_sponsored(SPONSOR_ADDR, OPERATOR_KEY, FACTORY_ADDR, dcd, *sponsor.DEPLOY_GAS)
        vault_deployed = bool(drc and drc.get("status") == "0x1") and len(rpc.get_code(dest)) > 2
        vault_deploy_tx = drc.get("transactionHash") if drc else None

    return {"ok": bool(ok), "tx": txh,
            "gas": int(receipt["gasUsed"], 16) if receipt else None,
            "block": int(receipt["blockNumber"], 16) if ok else None,
            "to_vault": True, "dest": dest,
            "vault_deployed": vault_deployed, "vault_deploy_tx": vault_deploy_tx,
            "bob_balance": eth(rpc.get_balance(dest)),
            "stealth_left": eth(rpc.get_balance(addr)),
            "code": len(rpc.get_code(addr)) // 2 - 1}


def api_decode(body):
    """Fetch a tx by hash and explain it frame by frame (the node already
    decodes 0x06 into JSON; we add the human-readable meaning)."""
    txh = body["tx"].strip()
    tx = rpc.rpc("eth_getTransactionByHash", [txh])
    return framedecode.explain(tx)


def api_balance(body):
    """Poll a stealth address: how much has arrived, and whether it holds enough
    above the self-paid gas reserve to be swept. Drives the UI deposit listener."""
    addr = body["addr"]
    wei = rpc.get_balance(addr)
    reserve = stealth.sweep_reserve_wei()
    return {"eth": eth(wei), "funded": wei > 0, "ready": wei > reserve,
            "reserve_eth": eth(reserve),
            "code": len(rpc.get_code(addr)) // 2 - 1}


VAULT_SCAN = 32   # how many indices to scan for a free slot / an imported address


def vault_root(auth):
    """Turn the client's chosen key source into the 32-byte owner root. Two paths:
    a passkey PRF secret (post-quantum, preferred) or, as a fallback, a wallet
    signature plus a user PIN (so a stolen k1 key alone cannot re-derive)."""
    method = (auth or {}).get("method")
    if method == "prf":
        return vault.prf_root(auth["secret"])
    if method == "wallet":
        pin = auth.get("pin") or ""
        if len(pin) < 6:
            raise RuntimeError("choose a PIN or passphrase of at least 6 characters for the k1 fallback")
        return vault.pin_root(auth["sig"], pin)
    raise RuntimeError("unknown key source")


def api_vault_create(body):
    """PREPARE a NEW PQ vault: derive its ML-DSA key from the owner root (passkey
    PRF, or wallet signature + PIN) at the first not-yet-deployed index, and return
    the CREATE2 deploy calldata. The CONNECTED WALLET sends it and pays gas.
    Deterministic, so it can be re-loaded later by importing its address."""
    b = BOBS[body["handle"]]
    if not FACTORY_ADDR:
        raise RuntimeError("no factory: run demo.py factory-deploy, set PQ_FACTORY")
    root = vault_root(body["auth"])
    for i in range(VAULT_SCAN):
        vkey = vault.VaultKey.from_root(root, i)
        addr = vault.predict_vault_address(FACTORY_ADDR, vkey.commit)
        if len(rpc.get_code(addr)) <= 2:
            b["vault_key"], b["vault"] = vkey, addr
            # COUNTERFACTUAL: derive and predict the address only, never deploy here.
            # Gasless deploys the vault at COLLECT (when the sweep lands in it); the
            # wallet path returns the calldata to deploy it later.
            out = {"vault": addr, "index": i, "factory": FACTORY_ADDR,
                   "deployed": False, "gasless": GASLESS}
            if not GASLESS:
                out["deploy_calldata"] = vault.deploy_calldata(vkey.commit)
            return out
    raise RuntimeError(f"all {VAULT_SCAN} vault slots are deployed; import one instead")


def api_vault_import(body):
    """Load an existing vault by address, checking the connected wallet owns it:
    scan the owner root's derived vaults and match the pasted address. On a match
    the key is re-derived (so withdraws work); no match means not the owner."""
    b = BOBS[body["handle"]]
    root, target = vault_root(body["auth"]), body["address"].strip().lower()
    for i in range(VAULT_SCAN):
        vkey = vault.VaultKey.from_root(root, i)
        addr = vault.predict_vault_address(FACTORY_ADDR, vkey.commit)
        if addr.lower() == target:
            b["vault_key"], b["vault"] = vkey, addr
            deployed = len(rpc.get_code(addr)) > 2
            return {"owner": True, "vault": addr, "index": i, "deployed": deployed,
                    "eth": eth(rpc.get_balance(addr)),
                    "deploy_calldata": None if deployed else vault.deploy_calldata(vkey.commit),
                    "factory": FACTORY_ADDR}
    return {"owner": False, "vault": body["address"]}


def api_vault_status(body):
    b = BOBS.get(body["handle"], {})
    addr = b.get("vault")
    if not addr:
        return {"vault": None}
    wei = rpc.get_balance(addr)
    return {"vault": addr, "eth": eth(wei), "funded": wei > 0,
            "deployed": len(rpc.get_code(addr)) > 2}


def api_vault_withdraw(body):
    """Sign the ML-DSA authorization over (dest, amount, ...) and move the funds.
    When gasless, the sponsor pays and the backend broadcasts; otherwise the
    calldata is returned for the connected wallet to submit. Either way the
    signature fixes dest and amount, so neither the sponsor nor the wallet can
    steal or redirect."""
    b = BOBS[body["handle"]]
    addr, vkey = b["vault"], b["vault_key"]
    # The destination must be supplied (the connected wallet). Never default to a
    # throwaway address: fresh_eoa() discards its key, so those funds are lost.
    dest = (body.get("dest") or "").strip()
    if not dest.startswith("0x") or len(dest) != 42:
        raise RuntimeError("withdraw needs a destination address (your connected wallet)")
    amount = rpc.get_balance(addr)
    nonce = vault.vault_nonce(addr)
    sig = vault.sign_withdraw(vkey, addr, dest, amount, nonce)
    calldata = vault.withdraw_calldata(dest, amount, vkey.pk_deploy, sig)
    if GASLESS:
        rc = sponsor.relay_sponsored(SPONSOR_ADDR, OPERATOR_KEY, addr, calldata, *sponsor.WITHDRAW_GAS)
        ok = bool(rc and rc.get("status") == "0x1")
        return {"vault": addr, "gasless": True, "dest": dest, "amount_eth": eth(amount),
                "ok": ok, "tx": rc.get("transactionHash") if rc else None,
                "bob_balance": eth(rpc.get_balance(dest))}
    return {"vault": addr, "to": addr, "calldata": "0x" + calldata.hex(), "gasless": False,
            "dest": dest, "amount_eth": eth(amount)}


def api_vault_plain_withdraw(body):
    """Build a plain withdraw with the vault's PUBLIC key but NO valid signature,
    and simulate it (free eth_call) to show it reverts. Returns the calldata so the
    connected wallet can also send the real (failing) call. The point: a normal k1
    withdraw cannot move PQ-protected funds."""
    b = BOBS[body["handle"]]
    addr, vkey = b["vault"], b["vault_key"]
    dest = body.get("dest") or addr
    amount = rpc.get_balance(addr)
    data = vault.unsigned_withdraw_calldata(addr, dest, amount, vkey.pk_deploy)
    try:
        rpc.eth_call({"to": addr, "data": data})
        revert = None  # should never happen
    except rpc.RpcError as e:
        revert = str(e)
    return {"vault": addr, "to": addr, "calldata": data, "reverted": revert is not None,
            "revert": revert, "amount_eth": eth(amount)}


ROUTES = {
    "/api/config": api_config,
    "/api/bob/new": api_bob_new,
    "/api/alice/derive": api_alice_derive,
    "/api/alice/pay": api_alice_pay,
    "/api/bob/collect": api_bob_collect,
    "/api/decode": api_decode,
    "/api/balance": api_balance,
    "/api/vault/create": api_vault_create,
    "/api/vault/import": api_vault_import,
    "/api/vault/status": api_vault_status,
    "/api/vault/withdraw": api_vault_withdraw,
    "/api/vault/plain-withdraw": api_vault_plain_withdraw,
}


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *_):
        pass

    def _send(self, code, body, ctype="application/json"):
        data = body if isinstance(body, bytes) else json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        path = self.path.split("?", 1)[0]
        rel = "index.html" if path in ("/", "/index.html") else path.lstrip("/")
        return self._serve_static(rel)

    def _serve_static(self, rel):
        """Serve an asset from the ui/ directory (index.html, styles.css, js/*.js).
        Confined to HERE, and only the asset extensions in STATIC_TYPES, so sibling
        files like server.py are never served. Rejects traversal outside HERE."""
        rel = os.path.normpath(rel)
        full = os.path.join(HERE, rel)
        ctype = STATIC_TYPES.get(os.path.splitext(full)[1])
        if (ctype is None or rel.startswith("..") or os.path.isabs(rel)
                or not os.path.isfile(full)
                or not os.path.realpath(full).startswith(os.path.realpath(HERE) + os.sep)):
            return self._send(404, {"error": "not found"})
        with open(full, "rb") as f:
            self._send(200, f.read(), ctype)

    def do_POST(self):
        fn = ROUTES.get(self.path)
        if not fn:
            return self._send(404, {"error": "not found"})
        try:
            n = int(self.headers.get("Content-Length", 0))
            body = json.loads(self.rfile.read(n) or b"{}") if n else {}
            self._send(200, fn(body))
        except Exception as e:  # noqa: BLE001, surface errors to the UI
            self._send(500, {"error": f"{type(e).__name__}: {e}"})

    # config is a GET convenience too
    def do_config(self):
        pass


def main():
    if not FACTORY_ADDR:
        print("WARNING: no PQ_FACTORY set; run `python demo.py factory-deploy` first.")
    if FUNDER_KEY:
        print(f"pq-dksap UI on http://localhost:{PORT}   (Alice {alice()[1]}, chain {CHAIN_ID})")
    else:
        print(f"pq-dksap UI on http://localhost:{PORT}   (external-payment mode: pay the QR "
              f"from any wallet; chain {CHAIN_ID})")
    ThreadingHTTPServer(("127.0.0.1", PORT), Handler).serve_forever()


if __name__ == "__main__":
    main()
