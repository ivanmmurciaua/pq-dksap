#!/usr/bin/env python3
"""PoC step 2: signature-GATED Sponsor-as-sender on Hegota (EIP-8141, chain 8141).

Step 1 (poc_sponsor.py) proved an always-approve sponsor pays for a DEFAULT call
into another contract. This adds the gate: the Sponsor (contracts/Sponsor.yul)
approves ONLY IF the operator signed this exact transaction. Its VERIFY code reads
the canonical sig_hash (TXPARAM 0x08), reads an inline ECDSA signature from the
ARBITRARY signature entry (SIGDATACOPY), ecrecovers it, and APPROVEs scope 3 only
when the recovered address equals the operator (a 32-byte constructor argument in
storage). The operator signature binds sig_hash, so it authorizes exactly this
tx's frames and cannot be replayed onto another.

Two checks:
  POSITIVE: the operator signs   -> sponsor approves, sponsor pays, target flips.
  NEGATIVE: a wrong key signs     -> sponsor reverts, nobody's gas is burned, the
                                     target is untouched. Proves the gate gates.

Run:
    export PQ_FUNDER_KEY=0x<funded key>      # also acts as the operator here
    python scripts/poc_sponsor_gated.py
"""
import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from eth_keys import keys                                       # noqa: E402
from eth_utils import to_canonical_address                      # noqa: E402

from pq_dksap import rpc                                        # noqa: E402
from pq_dksap.config import CHAIN_ID                            # noqa: E402
from pq_dksap.legacytx import sign_legacy, create_address       # noqa: E402
from pq_dksap.frametx import (                                  # noqa: E402
    Frame, FrameTx, Signature, MODE_VERIFY, MODE_DEFAULT,
    FLAG_APPROVE_EXECUTION_AND_PAYMENT, FLAG_NONE, SCHEME_ARBITRARY,
)

HERE = os.path.dirname(__file__)
TARGET_RUNTIME = bytes([0x60, 0x01, 0x60, 0x00, 0x55, 0x00])   # PUSH1 1;PUSH1 0;SSTORE;STOP
FUND_SPONSOR = 5 * 10 ** 16                                     # 0.05 ETH


def sponsor_creation():
    with open(os.path.join(HERE, "..", "contracts", "Sponsor.bin")) as f:
        return bytes.fromhex(f.read().strip())


def init_code(runtime: bytes) -> bytes:
    n = len(runtime)
    return bytes([0x60, n, 0x60, 0x0c, 0x60, 0x00, 0x39,
                  0x60, n, 0x60, 0x00, 0xf3]) + runtime


def wait(txh, timeout=180):
    t0 = time.time()
    while time.time() - t0 < timeout:
        rc = rpc.get_receipt(txh)
        if rc:
            return rc
        time.sleep(2)
    return None


def gas_price():
    return rpc.gas_price() * 2 + 1


def send_deploy(funder_hex, funder, data, value, label):
    n = rpc.get_nonce(funder)
    addr = create_address(funder, n)
    try:
        gas = rpc.estimate_gas({"from": funder, "data": "0x" + data.hex(), "value": hex(value)})
    except Exception:
        gas = 400_000
    raw, _ = sign_legacy(funder_hex, nonce=n, gas_price=gas_price(), gas_limit=gas + 120_000,
                         to=None, value=value, data=data, chain_id=CHAIN_ID)
    rc = wait(rpc.send_raw(raw))
    ok = rc and rc.get("status") == "0x1"
    print(f"-> {label} {addr}  status {rc.get('status') if rc else 'none'}  "
          f"code {len(rpc.get_code(addr))//2 - 1}B  bal {rpc.get_balance(addr)/10**18:.4f} ETH")
    if not ok:
        sys.exit(f"{label} deploy failed")
    return addr


def storage0(addr):
    return int(rpc.rpc("eth_getStorageAt", [addr, "0x0", "latest"]), 16)


def build_tx(sponsor, target):
    return FrameTx(
        chain_id=CHAIN_ID, nonce=rpc.get_nonce(sponsor), sender=sponsor,
        frames=[
            Frame(mode=MODE_VERIFY, flags=FLAG_APPROVE_EXECUTION_AND_PAYMENT,
                  target=None, execution_gas=80_000, state_gas=50_000, value=0, data=b""),
            Frame(mode=MODE_DEFAULT, flags=FLAG_NONE,
                  target=target, execution_gas=120_000, state_gas=120_000, value=0, data=b""),
        ],
        signatures=[Signature(scheme=SCHEME_ARBITRARY, signer=None, msg=b"", signature=b"")],
        max_priority_fee_per_gas=10 ** 8,
        max_fee_per_gas=rpc.base_fee() * 2 + 10 ** 8,
    )


def sign_operator(tx, signer_hex):
    """Sign the tx's canonical sig_hash and place r||s||v (v=27/28) inline in the
    ARBITRARY entry, the exact bytes Sponsor.yul ecrecovers."""
    sk = keys.PrivateKey(bytes.fromhex(signer_hex.removeprefix("0x")))
    sig = sk.sign_msg_hash(tx.sig_hash())
    tx.signatures[0].signature = (sig.r.to_bytes(32, "big") + sig.s.to_bytes(32, "big")
                                  + bytes([sig.v + 27]))
    return tx


def main():
    funder_hex = os.environ.get("PQ_FUNDER_KEY", "")
    if not funder_hex:
        sys.exit("set PQ_FUNDER_KEY (funded key; also the operator here)")
    funder = keys.PrivateKey(bytes.fromhex(funder_hex.removeprefix("0x"))).public_key.to_checksum_address()
    operator_hex, operator = funder_hex, funder   # the operator is the funder in this PoC
    print(f"funder/operator {operator}  balance {rpc.get_balance(funder)/10**18:.5f} ETH  chain {CHAIN_ID}")

    print("\n=== deploy the sig-gated SPONSOR (operator baked in, endowed) ===")
    op_word = b"\x00" * 12 + to_canonical_address(operator)      # 32-byte constructor arg
    sponsor = send_deploy(funder_hex, funder, sponsor_creation() + op_word, FUND_SPONSOR, "sponsor")
    print("=== deploy the trivial TARGET ===")
    target = send_deploy(funder_hex, funder, init_code(TARGET_RUNTIME), 0, "target ")

    # ---- POSITIVE: the operator signs -> approve, pay, target flips -----------
    print(f"\n[POSITIVE] operator signs. target slot0 before: {storage0(target)}")
    sponsor_before, funder_before = rpc.get_balance(sponsor), rpc.get_balance(funder)
    tx = sign_operator(build_tx(sponsor, target), operator_hex)
    try:
        txh = rpc.send_raw(tx.encode_hex())
    except rpc.RpcError as e:
        sys.exit(f"  positive REJECTED (should have passed): {e}")
    rc = wait(txh)
    slot = storage0(target)
    sponsor_paid = sponsor_before - rpc.get_balance(sponsor)
    funder_delta = funder_before - rpc.get_balance(funder)
    frs = rc.get("frameReceipts") if rc else None
    print(f"  tx {txh}")
    print(f"  status {rc.get('status') if rc else 'none'}  frames {frs}  slot0 after {slot}")
    print(f"  sponsor paid {sponsor_paid/10**18:.6f} ETH  |  funder delta {funder_delta/10**18:.6f} ETH")
    pos_ok = rc and rc.get("status") == "0x1" and slot == 1 and sponsor_paid > 0 and funder_delta == 0

    # ---- NEGATIVE: a wrong key signs -> sponsor reverts, target untouched -----
    print("\n[NEGATIVE] a WRONG key signs (should revert / be rejected)")
    wrong_hex = "0x" + ("11" * 32)
    slot_before_neg = storage0(target)
    txn = sign_operator(build_tx(sponsor, target), wrong_hex)
    neg_rejected = False
    neg_status = None
    try:
        nh = rpc.send_raw(txn.encode_hex())
        rcn = wait(nh)
        neg_status = rcn.get("status") if rcn else None
        print(f"  tx {nh}  status {neg_status}  frames {rcn.get('frameReceipts') if rcn else None}")
    except rpc.RpcError as e:
        neg_rejected = True
        print(f"  node REJECTED it (expected): {str(e)[:120]}")
    slot_after_neg = storage0(target)
    neg_ok = (neg_rejected or neg_status == "0x0") and slot_after_neg == slot_before_neg

    print("\n==============================")
    print(f"POSITIVE {'OK' if pos_ok else 'FAIL'}  |  NEGATIVE {'OK' if neg_ok else 'FAIL'}")
    if pos_ok and neg_ok:
        print("PROVEN: the operator-gated sponsor pays for a call ONLY when the")
        print("operator signed this exact tx; a wrong signature is refused. Next:")
        print("point the DEFAULT frame at the factory (deploy) and vault.withdraw.")
    print("==============================")
    return 0 if (pos_ok and neg_ok) else 1


if __name__ == "__main__":
    sys.exit(main())
