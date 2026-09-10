#!/usr/bin/env python3
"""PoC: Sponsor-as-sender gasless plumbing on Hegota (EIP-8141, chain 8141).

This proves the exact payment path our post-quantum gasless vault needs, the ONE
frame shape not yet exercised onchain:

  - a SPONSOR contract is the frame-tx SENDER; its VERIFY code runs APPROVE with
    scope 3 (execution + payment), so the SPONSOR pays the whole transaction gas;
  - a DEFAULT frame calls a DIFFERENT target contract (here a trivial one that
    SSTOREs a flag), the shape of `factory.deploy` and `vault.withdraw`;
  - no EOA signs or funds the frame tx: the end user would put ZERO ETH.

Why the sponsor must BE the sender (verified in ethrex `eip8141-v2`,
crates/vm/levm/src/opcode_handlers/frame_tx.rs): APPROVE payment sets
`payer = frame.target` with NO `payer == tx.sender` check (so a third party can
pay), BUT execution approval (scope 2/3) requires `frame_target == tx.sender`,
and payment approval requires execution approved first. So the sponsor
self-approves (scope 3) as the sender, then a DEFAULT frame calls the target.

This PoC uses an ALWAYS-APPROVE sponsor (no gate) to prove the plumbing. The real
one replaces that 7-byte runtime with a signature-gated approve (operator
ecrecover in the 100k VERIFY prefix), and points the DEFAULT frame at the factory
(deploy) or the vault (withdraw with the inline ML-DSA signature).

Run:
    export PQ_FUNDER_KEY=0x<funded key>       # only to deploy the two contracts
    python scripts/poc_sponsor.py
"""
import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from eth_keys import keys                                       # noqa: E402

from pq_dksap import rpc                                        # noqa: E402
from pq_dksap.config import CHAIN_ID                            # noqa: E402
from pq_dksap.legacytx import sign_legacy, create_address       # noqa: E402
from pq_dksap.frametx import (                                  # noqa: E402
    Frame, FrameTx, MODE_VERIFY, MODE_DEFAULT,
    FLAG_APPROVE_EXECUTION_AND_PAYMENT, FLAG_NONE,
)

# Always-approve sponsor runtime: PUSH1 3; PUSH1 0; PUSH1 0; APPROVE(0xAA).
# Scope 3 == execution + payment; target resolves to the sender (this contract),
# satisfying the `frame_target == tx.sender` rule, and sets payer = this contract.
SPONSOR_RUNTIME = bytes([0x60, 0x03, 0x60, 0x00, 0x60, 0x00, 0xAA])

# Trivial target runtime: PUSH1 1; PUSH1 0; SSTORE; STOP. Any call flips slot 0
# to 1, so we can read storage to prove the sponsored call actually executed.
TARGET_RUNTIME = bytes([0x60, 0x01, 0x60, 0x00, 0x55, 0x00])

FUND_SPONSOR = 5 * 10 ** 16   # 0.05 ETH: covers the frame-tx max cost it fronts


def init_code(runtime: bytes) -> bytes:
    """Constructor (12 bytes) that returns `runtime` (CODECOPY from offset 12)."""
    n = len(runtime)
    return bytes([0x60, n, 0x60, 0x0c, 0x60, 0x00, 0x39,
                  0x60, n, 0x60, 0x00, 0xf3]) + runtime


def load_funder():
    pk_hex = os.environ.get("PQ_FUNDER_KEY", "")
    if not pk_hex:
        sys.exit("set PQ_FUNDER_KEY (a funded key, only to deploy the two contracts)")
    addr = keys.PrivateKey(bytes.fromhex(pk_hex.removeprefix("0x"))).public_key.to_checksum_address()
    return pk_hex, addr


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


def deploy(funder_hex, funder, runtime, value, label):
    """CREATE a contract endowed with `value` (calling it later would run its code,
    so a CREATE endowment is the only clean way to give it a balance)."""
    data = init_code(runtime)
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
    code = rpc.get_code(addr)
    print(f"-> {label} {addr}  status {rc.get('status') if rc else 'none'}  "
          f"code {len(code)//2 - 1}B  bal {rpc.get_balance(addr)/10**18:.4f} ETH")
    if not ok or code.lower() != "0x" + runtime.hex():
        sys.exit(f"{label} deploy failed or code mismatch")
    return addr


def storage0(addr):
    return int(rpc.rpc("eth_getStorageAt", [addr, "0x0", "latest"]), 16)


def main():
    funder_hex, funder = load_funder()
    print(f"funder {funder}  balance {rpc.get_balance(funder)/10**18:.5f} ETH  chain {CHAIN_ID}")

    print("\n=== deploy the always-approve SPONSOR (endowed) ===")
    sponsor = deploy(funder_hex, funder, SPONSOR_RUNTIME, FUND_SPONSOR, "sponsor")
    print("\n=== deploy the trivial TARGET (SSTORE flag) ===")
    target = deploy(funder_hex, funder, TARGET_RUNTIME, 0, "target ")

    print(f"\ntarget slot0 before: {storage0(target)}  (expect 0)")
    sponsor_before = rpc.get_balance(sponsor)
    funder_before = rpc.get_balance(funder)

    # Sponsor is the sender; it self-approves scope 3 and pays; the DEFAULT frame
    # calls the (different) target contract. No signatures: the sponsor's code is
    # the authorization (the real one gates this on an operator signature).
    tx = FrameTx(
        chain_id=CHAIN_ID, nonce=rpc.get_nonce(sponsor), sender=sponsor,
        frames=[
            Frame(mode=MODE_VERIFY, flags=FLAG_APPROVE_EXECUTION_AND_PAYMENT,
                  target=None, execution_gas=60_000, state_gas=50_000, value=0, data=b""),
            Frame(mode=MODE_DEFAULT, flags=FLAG_NONE,
                  target=target, execution_gas=120_000, state_gas=120_000, value=0, data=b""),
        ],
        signatures=[],
        max_priority_fee_per_gas=10 ** 8,
        max_fee_per_gas=rpc.base_fee() * 2 + 10 ** 8,
    )

    print("\n=== sponsored frame tx (sponsor pays, DEFAULT calls the target) ===")
    try:
        txh = rpc.send_raw(tx.encode_hex())
    except rpc.RpcError as e:
        sys.exit(f"REJECTED by node: {e}")
    print(f"tx {txh}")
    rc = wait(tx and txh)
    if not rc:
        sys.exit("no receipt (timeout)")
    used = int(rc.get("gasUsed", "0x0"), 16)
    print(f"status {rc.get('status')}  gasUsed {used}  block {rc.get('blockNumber')}")
    frs = rc.get("frameReceipts")
    if frs:
        print(f"frames: {[(f.get('status'), int(f.get('gasUsed','0x0'),16)) for f in frs]}")

    slot = storage0(target)
    sponsor_paid = sponsor_before - rpc.get_balance(sponsor)
    funder_delta = funder_before - rpc.get_balance(funder)
    print(f"\ntarget slot0 after : {slot}  (expect 1)")
    print(f"sponsor paid       : {sponsor_paid/10**18:.6f} ETH  (gas came from the sponsor)")
    print(f"funder delta       : {funder_delta/10**18:.6f} ETH  (expect 0: the EOA paid nothing)")

    ok = rc.get("status") == "0x1" and slot == 1 and sponsor_paid > 0 and funder_delta == 0
    print("\n==============================")
    if ok:
        print("PROVEN: a SPONSOR (as sender) paid gas for a DEFAULT call into ANOTHER")
        print("contract. No EOA funded the tx. Next: gate the sponsor on an operator")
        print("signature, and point the DEFAULT frame at the factory / vault.withdraw.")
    else:
        print("NOT proven: check status / slot / balances above.")
    print("==============================")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
