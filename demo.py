#!/usr/bin/env python3
"""End-to-end console demo: a post-quantum stealth payment, Alice -> Bob.

The story:
  1. Bob prepares a one-time STEALTH address governed by a post-quantum (ML-DSA)
     key. Funds parked there can be moved only by Bob's ML-DSA signature.
  2. Alice PAYS into Bob's stealth address (she is any funded account).
  3. Bob SWEEPS the stealth to his own wallet, authorized on-chain by his ML-DSA
     signature, with the public key passed inline. No ECDSA in the spend.

Nothing is predefined: Bob's wallet, Bob's stealth key, and the amount are
generated fresh each run. Alice is whoever holds the funder key ($PQ_FUNDER_KEY
or ./.funder_key) with test ETH from https://faucet.privacy.ethrex.xyz/

This testnet mines in bursts and can stall for minutes, so the demo is RESUMABLE:
a run saves its state to ./.pqstate.json. If the sweep does not confirm in time,
re-run with `--resume` to pick up the SAME pending sweep (no redeploy, no new tx)
once the chain advances.

  NOTE ON HONESTY: in a full DKSAP, Alice DERIVES Bob's stealth address from Bob's
  published meta-address (blinded ML-DSA key agreement) so only Bob can spend and
  the two are unlinkable. That derivation is the cryptographic layer (see
  Acknowledgments); here Bob simply generates the stealth key. The on-chain
  mechanism demonstrated below is identical either way.

Usage:
  PQ_FUNDER_KEY=0x... python demo.py [--amount WEI] [--seed HEX]
  python demo.py --resume        # continue the last run's pending sweep
"""
import argparse
import json
import os
import secrets
import sys

from eth_abi import encode as abi_encode
from eth_keys import keys
from eth_utils import keccak

from pq_dksap import rpc, stealth
from pq_dksap.config import CHAIN_ID, SINGLETON

ETH = 10 ** 18
STATE_FILE = ".pqstate.json"
SWEEP_WAIT = 180   # seconds to wait for the sweep before suggesting --resume


def eth(wei):
    return f"{wei / ETH:.6f} ETH"


def fresh_eoa():
    return keys.PrivateKey(secrets.token_bytes(32)).public_key.to_checksum_address()


def load_alice():
    pk_hex = os.environ.get("PQ_FUNDER_KEY")
    if not pk_hex and os.path.exists(".funder_key"):
        pk_hex = open(".funder_key").read().strip()
    if not pk_hex:
        sys.exit("No funder key. Set $PQ_FUNDER_KEY or ./.funder_key with a test-ETH "
                 "secp256k1 key (faucet.privacy.ethrex.xyz).")
    addr = keys.PrivateKey(bytes.fromhex(pk_hex.removeprefix("0x"))).public_key.to_checksum_address()
    return pk_hex, addr


def save_state(**kw):
    json.dump(kw, open(STATE_FILE, "w"), indent=2)


def load_state():
    if not os.path.exists(STATE_FILE):
        sys.exit("Nothing to resume (no ./.pqstate.json). Run a fresh demo first.")
    return json.load(open(STATE_FILE))


def rule(title):
    print(f"\n\033[1m{title}\033[0m")


def sanity_verifier(key, tx, h):
    """Off-chain check that the shared singleton accepts this signature."""
    sig = tx.signatures[0].signature[len(key.pk_deploy):]
    isel = keccak(text="verifyInline(bytes,bytes32,bytes)")[:4]
    dbg = isel + abi_encode(["bytes", "bytes32", "bytes"], [key.pk_deploy, h, sig])
    return rpc.eth_call({"to": SINGLETON, "data": "0x" + dbg.hex(), "gas": hex(50_000_000)})[:10]


def report(bob_wallet, account, amount, rc, used=None):
    ok = rc.get("status") == "0x1"
    block = int(rc.get("blockNumber"), 16)
    used = used if used is not None else int(rc.get("gasUsed"), 16)
    frames = rc.get("frameReceipts")
    print(f"    confirmed: status {rc.get('status')}   gas {used:,}   block {block}")
    print(f"    (frame-tx hashes differ on the dora explorer; find it by block {block})")
    if frames:
        for lbl, f in zip(["VERIFY  (approve)", "DEFAULT (ml-dsa verify)", "SENDER  (move value)"], frames):
            print(f"      {lbl:<24} {f.get('status')}  {int(f.get('gasUsed','0x0'),16):>9,} gas")
    rule("outcome")
    print(f"  Bob's wallet : {bob_wallet}   {eth(rpc.get_balance(bob_wallet))}")
    print(f"  stealth left : {eth(rpc.get_balance(account))}   (gas change, unlinked to Bob)")
    print("\n" + "=" * 58)
    if ok:
        print("  OK  Alice paid Bob through a post-quantum stealth address.")
        print(f"      Only Bob's ML-DSA key could move it. Sweep gas: {used:,}")
    else:
        print("  FAILED  the sweep reverted; see status above.")
    print("=" * 58)
    return 0 if ok else 1


def do_sweep(key, account, bob_wallet, amount):
    """Build, authorize, and broadcast the sweep. Returns the tx hash."""
    tx = stealth.build_spend(account, bob_wallet, amount)
    h = stealth.authorize(tx, key)
    print(f"  Bob signs the sweep tx's sig_hash with his ML-DSA key")
    print(f"    sig_hash    : 0x{h.hex()}")
    print(f"    signature   : {len(tx.signatures[0].signature) - len(key.pk_deploy)} B  +  pk {len(key.pk_deploy)} B  (inline)")
    dret = sanity_verifier(key, tx, h)
    print(f"    verifier     : verifyInline -> {dret}  ({'valid' if dret == '0x024ad318' else 'INVALID'})")
    txh = rpc.send_raw(tx.encode_hex())
    print(f"  sweep tx sent : {txh}", flush=True)
    return txh


def finish_sweep(txh, bob_wallet, account, amount):
    """Wait for the sweep receipt; on timeout, keep state for --resume."""
    print(f"    waiting for a block ...", flush=True)
    rc = stealth._wait(txh, timeout=SWEEP_WAIT)
    if rc:
        return report(bob_wallet, account, amount, rc)
    print(f"\n  sweep not confirmed within {SWEEP_WAIT}s — the testnet is likely stalled.")
    print(f"  Nothing is lost: {eth(rpc.get_balance(account))} still sits in the stealth account,")
    print(f"  and the sweep {txh} is queued in the mempool.")
    print(f"  Re-run when the chain advances:  python demo.py --resume")
    return 2


def cmd_resume():
    st = load_state()
    key = stealth.StealthKey.from_seed(bytes.fromhex(st["seed"]))
    account, bob_wallet, amount = st["account"], st["bob_wallet"], st["amount"]
    rule("resume")
    print(f"  stealth acct : {account}   holds {eth(rpc.get_balance(account))}")
    print(f"  Bob's wallet : {bob_wallet}   {eth(rpc.get_balance(bob_wallet))}")

    txh = st.get("sweep_tx")
    if txh:
        rc = rpc.get_receipt(txh)
        if rc:
            print(f"  the pending sweep already confirmed:")
            return report(bob_wallet, account, amount, rc)
        known = rpc.rpc("eth_getTransactionByHash", [txh])
        if known:
            print(f"  pending sweep {txh} is still in the mempool; waiting ...")
            return finish_sweep(txh, bob_wallet, account, amount)
        print(f"  previous sweep dropped from the mempool; re-broadcasting ...")

    rule("Bob sweeps the stealth to his wallet (ML-DSA authorized)")
    txh = do_sweep(key, account, bob_wallet, amount)
    save_state(**{**st, "sweep_tx": txh})
    return finish_sweep(txh, bob_wallet, account, amount)


def cmd_fresh(args):
    alice_hex, alice = load_alice()
    bob_wallet = fresh_eoa()
    seed = bytes.fromhex(args.seed) if args.seed else secrets.token_bytes(32)
    key = stealth.StealthKey.from_seed(seed)

    rule("cast")
    print(f"  network      : ethrex Hegota privacy testnet  (chain {CHAIN_ID} / {hex(CHAIN_ID)})")
    print(f"  Alice (payer): {alice}   {eth(rpc.get_balance(alice))}")
    print(f"  Bob (wallet) : {bob_wallet}   {eth(rpc.get_balance(bob_wallet))}   [freshly generated]")
    print(f"  verifier     : {SINGLETON}   [shared singleton, ML-DSA on-chain]")

    endow = args.amount + args.gas_allowance
    if rpc.get_balance(alice) < endow + 10 ** 16:
        sys.exit("Alice's balance is too low. Top up at the faucet.")

    rule("1. Bob prepares a stealth address (post-quantum)")
    print(f"  scheme        : ML-DSA-44 (Dilithium2, ETH variant)")
    print(f"  stealth seed  : 0x{seed.hex()}   [Bob's; would come from an ML-KEM exchange]")
    print(f"  key commitment: 0x{key.commit.hex()}")
    print(f"  public key    : {len(key.pk_deploy)} bytes (expanded, travels inline at spend)")

    rule("2. Alice pays Bob's stealth address")
    print(f"  Alice endows the stealth account with {eth(endow)}")
    print(f"    = {eth(args.amount)} payment + {eth(args.gas_allowance)} sweep-gas allowance")
    account, rc = stealth.deploy_account(alice_hex, alice, key, endow)
    if not rc or rc.get("status") != "0x1":
        sys.exit(f"  payment failed: {rc}")
    save_state(seed=seed.hex(), account=account, bob_wallet=bob_wallet, amount=args.amount)
    print(f"  stealth addr  : {account}")
    print(f"    code {len(rpc.get_code(account))//2 - 1} B   holds {eth(rpc.get_balance(account))}")
    print(f"    deploy gas    : {int(rc.get('gasUsed'),16):,}   block {int(rc.get('blockNumber'),16)}")

    rule("3. Bob sweeps the stealth to his wallet (ML-DSA authorized)")
    txh = do_sweep(key, account, bob_wallet, args.amount)
    save_state(seed=seed.hex(), account=account, bob_wallet=bob_wallet, amount=args.amount, sweep_tx=txh)
    return finish_sweep(txh, bob_wallet, account, args.amount)


def main():
    ap = argparse.ArgumentParser(description="Post-quantum stealth payment demo (Alice -> Bob).")
    ap.add_argument("--resume", action="store_true", help="continue the last run's pending sweep (no redeploy)")
    ap.add_argument("--amount", type=int, default=10 ** 14, help="amount Alice pays Bob (wei, default 0.0001 ETH)")
    ap.add_argument("--seed", help="Bob's stealth key seed (hex, 32 bytes); random if omitted")
    ap.add_argument("--gas-allowance", type=int, default=3 * 10 ** 15,
                    help="extra wei Alice endows to cover Bob's on-chain sweep gas (sponsored in production)")
    args = ap.parse_args()
    return cmd_resume() if args.resume else cmd_fresh(args)


if __name__ == "__main__":
    sys.exit(main())
