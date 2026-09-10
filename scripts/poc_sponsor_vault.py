#!/usr/bin/env python3
"""PoC step 3: a real ML-DSA vault, DEPLOYED and WITHDRAWN gasless via the sponsor.

Ties the pieces together on Hegota (chain 8141):
  1. deploy + endow the sig-gated sponsor (operator baked in);
  2. derive a vault ML-DSA key and predict its CREATE2 address;
  3. SPONSORED DEPLOY: a frame tx where the sponsor pays and a DEFAULT frame calls
     the factory to CREATE2 the vault, so the vault costs the user zero ETH;
  4. fund the vault (stands in for the stealth sweep landing in it);
  5. SPONSORED WITHDRAW: a frame tx where the sponsor pays and a DEFAULT frame calls
     vault.withdraw with the inline ML-DSA signature, moving the funds to a fresh
     address, again for zero user ETH. The ML-DSA signature (not the sponsor)
     authorizes the spend, so custody stays post-quantum; the sponsor only pays gas
     and, gated by the operator signature, cannot be griefed.

Run:
    export PQ_FUNDER_KEY=0x<funded key>       # deployer + operator + vault funder
    export PQ_FACTORY=0x<create2 factory>     # from demo.py factory-deploy
    python scripts/poc_sponsor_vault.py
"""
import os
import secrets
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from eth_keys import keys                                       # noqa: E402

from pq_dksap import rpc, sponsor, vault                        # noqa: E402
from pq_dksap.config import CHAIN_ID, FACTORY                   # noqa: E402
from pq_dksap.legacytx import sign_legacy                       # noqa: E402
from pq_dksap.stealth import legacy_gas_price, _wait            # noqa: E402

FACTORY_ADDR = os.environ.get("PQ_FACTORY", FACTORY)
ENDOW_SPONSOR = 5 * 10 ** 16     # 0.05 ETH for the sponsor to front gas
FUND_VAULT = 10 ** 16            # 0.01 ETH into the vault
ETH = 10 ** 18

# The DEFAULT frame must fit the operation: a fresh-contract CREATE2 (big state
# charge) and the ~5M-gas ML-DSA verify inside withdraw.
DEPLOY_GAS = (3_000_000, 3_000_000)
WITHDRAW_GAS = (6_000_000, 1_000_000)


def fund(funder_hex, funder, to, amount, label):
    try:
        gas = rpc.estimate_gas({"from": funder, "to": to, "value": hex(amount)})
    except Exception:
        gas = 60_000
    raw, _ = sign_legacy(funder_hex, nonce=rpc.get_nonce(funder), gas_price=legacy_gas_price(),
                         gas_limit=gas + 20_000, to=to, value=amount, data=b"", chain_id=CHAIN_ID)
    rc = _wait(rpc.send_raw(raw))
    print(f"-> {label}: {'ok' if rc and rc.get('status')=='0x1' else 'FAIL'}  "
          f"({to} now {rpc.get_balance(to)/ETH:.4f} ETH)")
    return rc


def code_len(addr):
    return len(rpc.get_code(addr)) // 2 - 1


def main():
    funder_hex = os.environ.get("PQ_FUNDER_KEY", "")
    if not funder_hex:
        sys.exit("set PQ_FUNDER_KEY")
    if not FACTORY_ADDR:
        sys.exit("set PQ_FACTORY (run demo.py factory-deploy)")
    funder = keys.PrivateKey(bytes.fromhex(funder_hex.removeprefix("0x"))).public_key.to_checksum_address()
    operator_hex = funder_hex   # the backend operator is the funder in this PoC
    print(f"operator/funder {funder}  bal {rpc.get_balance(funder)/ETH:.4f} ETH  "
          f"factory {FACTORY_ADDR}  chain {CHAIN_ID}")

    print("\n=== deploy + endow the sponsor ===")
    sp, rc = sponsor.deploy_sponsor(funder_hex, funder, funder, ENDOW_SPONSOR)
    print(f"-> sponsor {sp}  status {rc.get('status') if rc else 'none'}  "
          f"code {code_len(sp)}B  bal {rpc.get_balance(sp)/ETH:.4f} ETH")
    if not rc or rc.get("status") != "0x1":
        return 1

    print("\n=== derive vault key + predict address ===")
    vkey = vault.VaultKey.from_seed(secrets.token_bytes(32))
    vaddr = vault.predict_vault_address(FACTORY_ADDR, vkey.commit)
    print(f"-> vault {vaddr}  commit 0x{vkey.commit.hex()[:16]}…  code {code_len(vaddr)}B (counterfactual)")

    # ---- SPONSORED DEPLOY -----------------------------------------------------
    print("\n=== SPONSORED DEPLOY (sponsor pays, DEFAULT frame calls the factory) ===")
    sp_before = rpc.get_balance(sp)
    deploy_calldata = vkey.commit + vault.vault_init_code(vkey.commit)   # salt || init_code
    rc = sponsor.relay_sponsored(sp, operator_hex, FACTORY_ADDR, deploy_calldata, *DEPLOY_GAS)
    print(f"-> status {rc.get('status') if rc else 'none'}  frames "
          f"{[f.get('status') for f in (rc.get('frameReceipts') or [])]}  vault code {code_len(vaddr)}B")
    deployed = code_len(vaddr) > 0
    if not deployed:
        print("   deploy did not land; aborting"); return 1

    print("\n=== fund the vault (stands in for the stealth sweep) ===")
    fund(funder_hex, funder, vaddr, FUND_VAULT, "fund vault")

    # ---- SPONSORED WITHDRAW ---------------------------------------------------
    print("\n=== SPONSORED WITHDRAW (sponsor pays, DEFAULT frame calls vault.withdraw) ===")
    dest = keys.PrivateKey(secrets.token_bytes(32)).public_key.to_checksum_address()
    amount = rpc.get_balance(vaddr)
    nonce = vault.vault_nonce(vaddr)
    sig = vault.sign_withdraw(vkey, vaddr, dest, amount, nonce)
    calldata = vault.withdraw_calldata(dest, amount, vkey.pk_deploy, sig)
    dest_before = rpc.get_balance(dest)
    funder_before = rpc.get_balance(funder)
    rc = sponsor.relay_sponsored(sp, operator_hex, vaddr, calldata, *WITHDRAW_GAS)
    dest_after = rpc.get_balance(dest)
    print(f"-> status {rc.get('status') if rc else 'none'}  frames "
          f"{[f.get('status') for f in (rc.get('frameReceipts') or [])]}")
    print(f"   dest received {(dest_after - dest_before)/ETH:.6f} ETH  (wanted {amount/ETH:.6f})")

    sp_paid = sp_before - rpc.get_balance(sp)
    funder_delta = funder_before - rpc.get_balance(funder)
    print(f"\nsponsor paid across deploy+withdraw: {sp_paid/ETH:.6f} ETH")
    print(f"funder delta during the withdraw   : {funder_delta/ETH:.6f} ETH  (expect 0)")

    ok = (deployed and rc and rc.get("status") == "0x1"
          and dest_after - dest_before == amount and funder_delta == 0 and sp_paid > 0)
    print("\n==============================")
    if ok:
        print("PROVEN end to end: an ML-DSA vault was DEPLOYED and WITHDRAWN with the")
        print("sponsor paying all gas. Zero user ETH; custody stayed post-quantum (the")
        print("ML-DSA signature, not the sponsor, authorized the spend).")
    else:
        print("NOT proven: check statuses / balances above.")
    print("==============================")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
