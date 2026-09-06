#!/usr/bin/env python3
"""Post-quantum DUAL-KEY stealth payment demo, Alice -> Bob.

Two ways to run it:

  * NON-INTERACTIVE (one process, the whole DKSAP flow automatically):
        PQ_FUNDER_KEY=0x...  python demo.py auto
        python demo.py                     # 'auto' is the default

  * INTERACTIVE / MULTI-TERMINAL (each role on its own terminal or machine;
    the two shared files can be copied between them):
        # Bob's terminal:
        python demo.py bob-init                 -> writes bob.meta.json (public)
                                                   + .bob_secret.json (private)
        # Alice's terminal (needs bob.meta.json + $PQ_FUNDER_KEY):
        python demo.py alice-pay --meta bob.meta.json
                                                -> writes payment.ann.json
        # Bob's terminal (needs payment.ann.json + .bob_secret.json):
        python demo.py bob-sweep --ann payment.ann.json

The dual-key protocol (see pq_dksap/dksap.py): Bob publishes a META-ADDRESS.
Alice, from public data only, DERIVES a one-time stealth address and pays it,
then hands Bob the ML-KEM ciphertext (the "announcement") OFF-CHAIN. Only Bob,
with his spending secrets, can form the blinded ML-DSA key that sweeps it. On
chain the two are unlinkable. Registry + on-chain scanning are future work.

Alice is whoever holds the funder key ($PQ_FUNDER_KEY or ./.funder_key) with
test ETH from https://faucet.privacy.ethrex.xyz/ . The stealth SWEEP itself is
pure ML-DSA verified on-chain, no ECDSA.
"""
import argparse
import json
import os
import secrets
import sys

from eth_abi import encode as abi_encode
from eth_keys import keys
from eth_utils import keccak

from pq_dksap import dksap, rpc, stealth
from pq_dksap.config import CHAIN_ID, FACTORY, SINGLETON

ETH = 10 ** 18
STATE_FILE = ".pqstate.json"
SECRET_FILE = ".bob_secret.json"
META_FILE = "bob.meta.json"
ANN_FILE = "payment.ann.json"
SWEEP_WAIT = 180


def eth(wei):
    return f"{wei / ETH:.6f} ETH"


def fresh_eoa():
    return keys.PrivateKey(secrets.token_bytes(32)).public_key.to_checksum_address()


def wallet_from(pk_hex=None):
    """Return (privkey_hex, checksummed address). Fresh key if pk_hex is None."""
    raw = bytes.fromhex(pk_hex.removeprefix("0x")) if pk_hex else secrets.token_bytes(32)
    k = keys.PrivateKey(raw)
    return raw.hex(), k.public_key.to_checksum_address()


def load_alice():
    pk_hex = os.environ.get("PQ_FUNDER_KEY")
    if not pk_hex and os.path.exists(".funder_key"):
        pk_hex = open(".funder_key").read().strip()
    if not pk_hex:
        sys.exit("No funder key. Set $PQ_FUNDER_KEY or ./.funder_key with a test-ETH "
                 "secp256k1 key (faucet.privacy.ethrex.xyz).")
    addr = keys.PrivateKey(bytes.fromhex(pk_hex.removeprefix("0x"))).public_key.to_checksum_address()
    return pk_hex, addr


def _dump(path, obj):
    json.dump(obj, open(path, "w"), indent=2)


def _load(path, what):
    if not os.path.exists(path):
        sys.exit(f"missing {what}: {path}")
    return json.load(open(path))


def rule(title):
    print(f"\n\033[1m{title}\033[0m")


def sanity_verifier(pk_deploy, sig, h):
    """Off-chain check that the shared singleton accepts this blinded signature."""
    isel = keccak(text="verifyInline(bytes,bytes32,bytes)")[:4]
    dbg = isel + abi_encode(["bytes", "bytes32", "bytes"], [pk_deploy, h, sig])
    return rpc.eth_call({"to": SINGLETON, "data": "0x" + dbg.hex(), "gas": hex(50_000_000)})[:10]


# --------------------------------------------------------------------------
# Deploy + sweep primitives (the on-chain engine is unchanged)
# --------------------------------------------------------------------------
def do_sweep(bkey, account, bob_wallet, amount):
    tx = stealth.build_spend(account, bob_wallet, amount)
    h = dksap.authorize(tx, bkey)
    sig = tx.signatures[0].signature[len(bkey.pk_deploy):]
    print(f"  Bob signs the sweep with his BLINDED ML-DSA key")
    print(f"    sig_hash    : 0x{h.hex()}")
    print(f"    signature   : {len(sig)} B  +  pk {len(bkey.pk_deploy)} B  (inline)")
    dret = sanity_verifier(bkey.pk_deploy, sig, h)
    print(f"    verifier     : verifyInline -> {dret}  ({'valid' if dret == '0x024ad318' else 'INVALID'})")
    txh = rpc.send_raw(tx.encode_hex())
    print(f"  sweep tx sent : {txh}", flush=True)
    return txh


def report(bob_wallet, account, rc, used=None):
    ok = rc.get("status") == "0x1"
    block = int(rc.get("blockNumber"), 16)
    used = used if used is not None else int(rc.get("gasUsed"), 16)
    print(f"    confirmed: status {rc.get('status')}   gas {used:,}   block {block}")
    print(f"    (frame-tx hashes differ on the dora explorer; find it by block {block})")
    rule("outcome")
    print(f"  Bob's wallet : {bob_wallet}   {eth(rpc.get_balance(bob_wallet))}")
    print(f"  stealth left : {eth(rpc.get_balance(account))}   (gas change, unlinked to Bob)")
    print("\n" + "=" * 58)
    if ok:
        print("  OK  Alice paid Bob through a post-quantum stealth address.")
        print(f"      Alice derived it; only Bob's blinded ML-DSA key moved it.")
        print(f"      Sweep gas: {used:,}")
    else:
        print("  FAILED  the sweep reverted; see status above.")
    print("=" * 58)
    return 0 if ok else 1


def finish_sweep(txh, bob_wallet, account, st):
    print(f"    waiting for a block ...", flush=True)
    rc = stealth._wait(txh, timeout=SWEEP_WAIT)
    if rc:
        return report(bob_wallet, account, rc)
    st["sweep_tx"] = txh
    _dump(STATE_FILE, st)
    print(f"\n  sweep not confirmed within {SWEEP_WAIT}s. Nothing is lost:")
    print(f"  {eth(rpc.get_balance(account))} still sits in the stealth account and")
    print(f"  the sweep {txh} is queued. Re-run the same command to resume.")
    return 2


# --------------------------------------------------------------------------
# Roles (interactive / multi-terminal)
# --------------------------------------------------------------------------
def cmd_bob_init(args):
    seed = bytes.fromhex(args.seed) if args.seed else secrets.token_bytes(32)
    meta_pub, _ = dksap.gen_meta(seed)
    wallet_key, bob_wallet = wallet_from(args.wallet)

    _dump(args.secret, {"seed": seed.hex(), "bob_wallet": bob_wallet,
                        "bob_wallet_key": wallet_key})
    _dump(args.out, {"meta": meta_pub.encode().hex(), "chain_id": CHAIN_ID})

    rule("Bob: prepare a post-quantum meta-address")
    print(f"  scheme        : ML-DSA-44 (Dilithium2) spend  +  ML-KEM-512 view")
    print(f"  meta-address  : {args.out}  ({len(meta_pub.encode())} B, PUBLIC - share with payers)")
    print(f"  Bob's wallet  : {bob_wallet}   (sweeps land here; its key is in the secret)")
    print(f"  private secret: {args.secret}  (seed + payout key; never share, never commit)")
    print(f"\n  Next: give {args.out} to Alice, then run:")
    print(f"        python demo.py alice-pay --meta {args.out}")
    return 0


def cmd_alice_pay(args):
    alice_hex, alice = load_alice()
    meta = _load(args.meta, "meta-address file")
    meta_pub = dksap.MetaPublic.decode(bytes.fromhex(meta["meta"]))

    rule("Alice: derive Bob's stealth address and pay it")
    print(f"  network      : ethrex Hegota privacy testnet  (chain {CHAIN_ID} / {hex(CHAIN_ID)})")
    print(f"  Alice (payer): {alice}   {eth(rpc.get_balance(alice))}")
    tgt = dksap.sender_derive(meta_pub)
    print(f"  derived from Bob's meta-address (public data only):")
    print(f"    stealth commit: 0x{tgt.commit.hex()}")
    print(f"    announcement  : ML-KEM ct {len(tgt.kem_ct)} B, view tag 0x{tgt.view_tag.hex()}")

    endow = args.amount + args.gas_allowance
    if rpc.get_balance(alice) < endow + 10 ** 16:
        sys.exit("Alice's balance is too low. Top up at the faucet.")
    print(f"  endowing the stealth account with {eth(endow)}")
    print(f"    = {eth(args.amount)} payment + {eth(args.gas_allowance)} sweep-gas allowance")
    account, rc = stealth.deploy_account(alice_hex, alice, tgt, endow)
    if not rc or rc.get("status") != "0x1":
        sys.exit(f"  payment failed: {rc}")
    print(f"  stealth addr  : {account}")
    print(f"    code {len(rpc.get_code(account))//2 - 1} B   holds {eth(rpc.get_balance(account))}")
    print(f"    deploy gas    : {int(rc.get('gasUsed'),16):,}   block {int(rc.get('blockNumber'),16)}")

    _dump(args.out, {"account": account, "amount": args.amount,
                     "kem_ct": tgt.kem_ct.hex(), "view_tag": tgt.view_tag.hex(),
                     "commit": tgt.commit.hex()})
    print(f"\n  announcement written to {args.out} (PUBLIC - hand it to Bob).")
    print(f"  Bob: python demo.py bob-sweep --ann {args.out}")
    return 0


def cmd_bob_sweep(args):
    secret = _load(args.secret, "Bob's secret file")
    ann = _load(args.ann, "announcement file")
    seed = bytes.fromhex(secret["seed"])
    bob_wallet = secret["bob_wallet"]
    account, amount = ann["account"], ann["amount"]

    meta_pub, meta_sec = dksap.gen_meta(seed)
    bkey = dksap.recipient_recover(meta_pub, meta_sec, bytes.fromhex(ann["kem_ct"]),
                                   view_tag=bytes.fromhex(ann["view_tag"]))
    if bkey.commit.hex() != ann["commit"]:
        sys.exit("recovered key does not match the announcement's commit "
                 "(wrong secret, or not this recipient's payment)")

    rule("Bob: sweep the stealth to his wallet (blinded ML-DSA)")
    print(f"  stealth acct : {account}   holds {eth(rpc.get_balance(account))}")
    print(f"  Bob's wallet : {bob_wallet}   {eth(rpc.get_balance(bob_wallet))}")
    print(f"  recovered the blinded spending key (commit matches announcement)")

    st = {"seed_present": True, "account": account, "bob_wallet": bob_wallet, "amount": amount}
    return _sweep_or_resume(bkey, account, bob_wallet, amount, st)


def _sweep_or_resume(bkey, account, bob_wallet, amount, st):
    prev = _load(STATE_FILE, "state") if os.path.exists(STATE_FILE) else {}
    txh = prev.get("sweep_tx") if prev.get("account") == account else None
    if txh:
        rc = rpc.get_receipt(txh)
        if rc:
            print(f"  the pending sweep already confirmed:")
            return report(bob_wallet, account, rc)
        if rpc.rpc("eth_getTransactionByHash", [txh]):
            print(f"  pending sweep {txh} still in the mempool; waiting ...")
            return finish_sweep(txh, bob_wallet, account, st)
        print(f"  previous sweep dropped; re-broadcasting ...")
    if rpc.get_balance(account) < amount:
        print(f"  stealth account holds < the payment; likely already swept.")
    txh = do_sweep(bkey, account, bob_wallet, amount)
    st["sweep_tx"] = txh
    _dump(STATE_FILE, st)
    return finish_sweep(txh, bob_wallet, account, st)


# --------------------------------------------------------------------------
# auto (non-interactive: the whole flow in one process)
# --------------------------------------------------------------------------
def cmd_auto(args):
    alice_hex, alice = load_alice()
    seed = bytes.fromhex(args.seed) if args.seed else secrets.token_bytes(32)
    bob_wallet = fresh_eoa()
    meta_pub, meta_sec = dksap.gen_meta(seed)
    # Bob publishes the meta-address; Alice works from the decoded (transported) copy.
    meta_pub_alice = dksap.MetaPublic.decode(meta_pub.encode())

    rule("cast")
    print(f"  network      : ethrex Hegota privacy testnet  (chain {CHAIN_ID} / {hex(CHAIN_ID)})")
    print(f"  Alice (payer): {alice}   {eth(rpc.get_balance(alice))}")
    print(f"  Bob (wallet) : {bob_wallet}   [freshly generated]")
    print(f"  verifier     : {SINGLETON}   [shared singleton, ML-DSA on-chain]")

    endow = args.amount + args.gas_allowance
    if rpc.get_balance(alice) < endow + 10 ** 16:
        sys.exit("Alice's balance is too low. Top up at the faucet.")

    rule("1. Bob publishes a post-quantum meta-address")
    print(f"  spend scheme  : ML-DSA-44 (Dilithium2)   view scheme: ML-KEM-512")
    print(f"  meta-address  : {len(meta_pub.encode())} B  (rho + full-precision t + KEM key)")

    rule("2. Alice derives a stealth address from the meta-address and pays it")
    tgt = dksap.sender_derive(meta_pub_alice)
    print(f"  Alice (public data only) computes the stealth address, cannot spend it:")
    print(f"    stealth commit: 0x{tgt.commit.hex()}")
    print(f"    announcement  : ML-KEM ct {len(tgt.kem_ct)} B (delivered to Bob off-chain)")
    print(f"  Alice endows the stealth account with {eth(endow)}")
    print(f"    = {eth(args.amount)} payment + {eth(args.gas_allowance)} sweep-gas allowance")
    account, rc = stealth.deploy_account(alice_hex, alice, tgt, endow)
    if not rc or rc.get("status") != "0x1":
        sys.exit(f"  payment failed: {rc}")
    print(f"  stealth addr  : {account}")
    print(f"    code {len(rpc.get_code(account))//2 - 1} B   holds {eth(rpc.get_balance(account))}")
    print(f"    deploy gas    : {int(rc.get('gasUsed'),16):,}   block {int(rc.get('blockNumber'),16)}")

    rule("3. Bob recovers the blinded key from the announcement and sweeps")
    bkey = dksap.recipient_recover(meta_pub, meta_sec, tgt.kem_ct, view_tag=tgt.view_tag)
    assert bkey.commit == tgt.commit, "recovered commit must match Alice's"
    print(f"  Bob decapsulates the ML-KEM ct, forms (s1+s', s2+e'), commit matches Alice's.")
    st = {"seed_present": True, "account": account, "bob_wallet": bob_wallet, "amount": args.amount}
    return _sweep_or_resume(bkey, account, bob_wallet, args.amount, st)


# --------------------------------------------------------------------------
# Counterfactual (CREATE2) flow: Alice funds a predictable address and walks
# away; Bob collects with a single self-paid deploy+sweep 0x06 tx (no gas of
# his own). This is the flow a QR/scan UI needs.
# --------------------------------------------------------------------------
def cmd_factory_deploy(args):
    alice_hex, alice = load_alice()
    rule("Deploy the CREATE2 factory (one-time)")
    addr, rc = stealth.deploy_factory(alice_hex, alice)
    if not rc or rc.get("status") != "0x1":
        sys.exit(f"  factory deploy failed: {rc}")
    print(f"  factory       : {addr}")
    print(f"    deploy gas    : {int(rc.get('gasUsed'),16):,}   block {int(rc.get('blockNumber'),16)}")
    print(f"\n  Reuse it: export PQ_FACTORY={addr}")
    return 0


def cmd_cf(args):
    factory = args.factory or FACTORY
    if not factory:
        sys.exit("No factory. Run `python demo.py factory-deploy`, then set $PQ_FACTORY "
                 "(or pass --factory 0x...).")
    alice_hex, alice = load_alice()
    seed = bytes.fromhex(args.seed) if args.seed else secrets.token_bytes(32)
    bob_wallet = fresh_eoa()
    meta_pub, meta_sec = dksap.gen_meta(seed)
    meta_alice = dksap.MetaPublic.decode(meta_pub.encode())

    rule("cast (counterfactual)")
    print(f"  network      : ethrex Hegota privacy testnet  (chain {CHAIN_ID} / {hex(CHAIN_ID)})")
    print(f"  Alice (payer): {alice}   {eth(rpc.get_balance(alice))}")
    print(f"  Bob (wallet) : {bob_wallet}   [freshly generated]")
    print(f"  factory      : {factory}   [CREATE2 deployer]")

    tgt = dksap.sender_derive(meta_alice)
    account = stealth.predict_stealth_address(factory, tgt.commit)
    endow = args.amount + args.gas_allowance
    if rpc.get_balance(alice) < endow + 10 ** 16:
        sys.exit("Alice's balance is too low. Top up at the faucet.")

    rule("1. Alice predicts Bob's stealth address (before it exists) and funds it")
    print(f"  stealth addr  : {account}   [CREATE2, no code yet: {len(rpc.get_code(account))//2 - 1} B]")
    print(f"  Alice sends {eth(endow)} with a plain transfer (no deploy), then walks away.")
    rc = stealth.fund_address(alice_hex, alice, account, endow)
    if not rc or rc.get("status") != "0x1":
        sys.exit(f"  funding failed: {rc}")
    print(f"    funded: holds {eth(rpc.get_balance(account))}   block {int(rc.get('blockNumber'),16)}")

    rule("2. Bob collects: ONE self-paid tx deploys the account AND sweeps")
    bkey = dksap.recipient_recover(meta_pub, meta_sec, tgt.kem_ct, view_tag=tgt.view_tag)
    assert bkey.commit == tgt.commit and stealth.predict_stealth_address(factory, bkey.commit) == account
    tx = stealth.build_deploy_spend(factory, account, tgt.commit, bob_wallet, args.amount)
    h = dksap.authorize(tx, bkey)
    sig = tx.signatures[0].signature[len(bkey.pk_deploy):]
    dret = sanity_verifier(bkey.pk_deploy, sig, h)
    print(f"  deploy+verify+send in one 0x06; Bob pays NO gas (the stealth self-pays).")
    print(f"    verifier      : verifyInline -> {dret}  ({'valid' if dret == '0x024ad318' else 'INVALID'})")
    txh = rpc.send_raw(tx.encode_hex())
    print(f"  tx sent       : {txh}", flush=True)
    print(f"    waiting for a block ...", flush=True)
    rc = stealth._wait(txh, timeout=SWEEP_WAIT)
    if not rc:
        print(f"  not confirmed within {SWEEP_WAIT}s; the funds sit safely at {account}.")
        return 2
    if rc.get("status") != "0x1":
        print(f"    reverted: status {rc.get('status')}  gas {int(rc.get('gasUsed'),16):,}  block {int(rc.get('blockNumber'),16)}")
        print(f"    (if the validation prefix rejected it, the deploy exceeded MAX_VERIFY_GAS; tune build_deploy_spend)")
        return 1
    print(f"    code now at stealth: {len(rpc.get_code(account))//2 - 1} B")
    return report(bob_wallet, account, rc)


# --------------------------------------------------------------------------
def main():
    ap = argparse.ArgumentParser(description="Post-quantum dual-key stealth payment (Alice -> Bob).")
    sub = ap.add_subparsers(dest="cmd")

    def add_amounts(p):
        p.add_argument("--amount", type=int, default=10 ** 14, help="wei Alice pays Bob (default 0.0001 ETH)")
        p.add_argument("--gas-allowance", type=int, default=3 * 10 ** 15,
                       help="extra wei to cover Bob's on-chain sweep gas (sponsored in production)")

    a = sub.add_parser("auto", help="run the whole DKSAP flow in one process (default)")
    add_amounts(a)
    a.add_argument("--seed", help="Bob's stealth seed (hex, 32 bytes); random if omitted")

    bi = sub.add_parser("bob-init", help="Bob: generate + publish a meta-address")
    bi.add_argument("--seed", help="Bob's master seed (hex, 32 bytes); random if omitted")
    bi.add_argument("--wallet", help="Bob's payout privkey (hex, 32 bytes); fresh key if omitted")
    bi.add_argument("--out", default=META_FILE, help="meta-address output file (public)")
    bi.add_argument("--secret", default=SECRET_FILE, help="Bob's private secret file")

    apay = sub.add_parser("alice-pay", help="Alice: derive Bob's stealth address and pay it")
    apay.add_argument("--meta", default=META_FILE, help="Bob's meta-address file")
    apay.add_argument("--out", default=ANN_FILE, help="announcement output file (public)")
    add_amounts(apay)

    bs = sub.add_parser("bob-sweep", help="Bob: recover the blinded key and sweep")
    bs.add_argument("--ann", default=ANN_FILE, help="announcement file from Alice")
    bs.add_argument("--secret", default=SECRET_FILE, help="Bob's private secret file")

    sub.add_parser("factory-deploy", help="deploy the CREATE2 factory once (then set $PQ_FACTORY)")

    cf = sub.add_parser("cf", help="counterfactual: fund a predicted address, collect with one self-paid deploy+sweep")
    cf.add_argument("--factory", help="CREATE2 factory address (else $PQ_FACTORY)")
    cf.add_argument("--seed", help="Bob's stealth seed (hex, 32 bytes); random if omitted")
    add_amounts(cf)

    args = ap.parse_args()
    cmd = args.cmd or "auto"
    if cmd == "auto":
        if not hasattr(args, "amount"):        # bare `python demo.py`
            args = ap.parse_args(["auto"])
        return cmd_auto(args)
    return {"bob-init": cmd_bob_init, "alice-pay": cmd_alice_pay,
            "bob-sweep": cmd_bob_sweep, "factory-deploy": cmd_factory_deploy,
            "cf": cmd_cf}[cmd](args)


if __name__ == "__main__":
    sys.exit(main())
