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

from pq_dksap import dksap, framedecode, rpc, stealth  # noqa: E402
from pq_dksap.config import CHAIN_ID, FACTORY         # noqa: E402
from eth_keys import keys                             # noqa: E402
from eth_utils import is_address, to_checksum_address  # noqa: E402

PORT = int(os.environ.get("PQ_UI_PORT", "8000"))
HERE = os.path.dirname(__file__)
ETH = 10 ** 18

FACTORY_ADDR = os.environ.get("PQ_FACTORY", FACTORY)
FUNDER_KEY = os.environ.get("PQ_FUNDER_KEY", "")

# In-memory demo state.
BOBS = {}        # handle -> {seed, bob_wallet, meta_pub, meta_sec}
PAYMENTS = {}    # stealth_address -> {handle, kem_ct, view_tag, amount}


def alice():
    if not FUNDER_KEY:
        raise RuntimeError("set PQ_FUNDER_KEY (Alice's test-ETH key)")
    return FUNDER_KEY, keys.PrivateKey(
        bytes.fromhex(FUNDER_KEY.removeprefix("0x"))).public_key.to_checksum_address()


def fresh_eoa():
    return keys.PrivateKey(secrets.token_bytes(32)).public_key.to_checksum_address()


def eth(wei):
    return f"{wei / ETH:.6f}"


# -- API handlers: each returns a JSON-able dict ---------------------------
def api_config(_):
    a = alice()[1] if FUNDER_KEY else None
    return {"chain_id": CHAIN_ID, "factory": FACTORY_ADDR, "alice": a,
            "alice_balance": eth(rpc.get_balance(a)) if a else None,
            "pay_button": bool(FUNDER_KEY)}


def api_bob_new(body):
    """Bob creates a post-quantum meta-address (his stealth identity). He may
    supply his own payout wallet; otherwise a throwaway one is generated."""
    handle = secrets.token_hex(4)
    seed = secrets.token_bytes(32)
    meta_pub, meta_sec = dksap.gen_meta(seed)
    payout = (body.get("payout") or "").strip()
    if payout:
        if not is_address(payout):
            raise ValueError("invalid payout address")
        bob_wallet, provided = to_checksum_address(payout), True
    else:
        bob_wallet, provided = fresh_eoa(), False
    BOBS[handle] = {"seed": seed, "bob_wallet": bob_wallet,
                    "meta_pub": meta_pub, "meta_sec": meta_sec}
    return {"handle": handle, "bob_wallet": bob_wallet, "provided": provided,
            "meta_bytes": len(meta_pub.encode())}


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
    tx = stealth.build_deploy_spend(FACTORY_ADDR, addr, p["commit"],
                                    b["bob_wallet"], amount)
    dksap.authorize(tx, bkey)
    txh = rpc.send_raw(tx.encode_hex())
    receipt = stealth._wait(txh, timeout=240)
    ok = receipt and receipt.get("status") == "0x1"
    return {"ok": bool(ok), "tx": txh,
            "gas": int(receipt["gasUsed"], 16) if receipt else None,
            "block": int(receipt["blockNumber"], 16) if ok else None,
            "bob_wallet": b["bob_wallet"],
            "bob_balance": eth(rpc.get_balance(b["bob_wallet"])),
            "stealth_left": eth(rpc.get_balance(addr)),
            "code": len(rpc.get_code(addr)) // 2 - 1}


def api_decode(body):
    """Fetch a tx by hash and explain it frame by frame (the node already
    decodes 0x06 into JSON; we add the human-readable meaning)."""
    txh = body["tx"].strip()
    tx = rpc.rpc("eth_getTransactionByHash", [txh])
    return framedecode.explain(tx)


ROUTES = {
    "/api/config": api_config,
    "/api/bob/new": api_bob_new,
    "/api/alice/derive": api_alice_derive,
    "/api/alice/pay": api_alice_pay,
    "/api/bob/collect": api_bob_collect,
    "/api/decode": api_decode,
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
        if self.path in ("/", "/index.html"):
            with open(os.path.join(HERE, "index.html"), "rb") as f:
                return self._send(200, f.read(), "text/html; charset=utf-8")
        self._send(404, {"error": "not found"})

    def do_POST(self):
        fn = ROUTES.get(self.path)
        if not fn:
            return self._send(404, {"error": "not found"})
        try:
            n = int(self.headers.get("Content-Length", 0))
            body = json.loads(self.rfile.read(n) or b"{}") if n else {}
            self._send(200, fn(body))
        except Exception as e:  # noqa: BLE001 — surface errors to the UI
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
