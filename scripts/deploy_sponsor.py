#!/usr/bin/env python3
"""One-time setup: deploy and fund the gasless SPONSOR on Hegota (chain 8141).

The sponsor pays gas for vault deploy/withdraw so the recipient needs zero ETH.
It is gated by an OPERATOR key (see contracts/Sponsor.yul): the backend signs each
sponsored tx and the sponsor's ecrecover checks it. The operator key holds NO
funds, only the sponsor contract does, so keep the sponsor topped up and the
operator key just secret.

Prints the two values the backend needs:
    PQ_SPONSOR       the deployed sponsor address
    PQ_OPERATOR_KEY  the operator signing key (generated here if you do not pass one)

Run:
    export PQ_FUNDER_KEY=0x<funded key>        # pays deploy gas + endows the sponsor
    export PQ_OPERATOR_KEY=0x<key>             # optional; generated if unset
    python scripts/deploy_sponsor.py [endow_eth]      # default 0.05
"""
import os
import secrets
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from eth_keys import keys                                   # noqa: E402

from pq_dksap import rpc, sponsor                           # noqa: E402
from pq_dksap.config import CHAIN_ID                        # noqa: E402

ETH = 10 ** 18


def main():
    funder_hex = os.environ.get("PQ_FUNDER_KEY", "")
    if not funder_hex:
        sys.exit("set PQ_FUNDER_KEY (funded key that pays deploy gas and endows the sponsor)")
    funder = keys.PrivateKey(bytes.fromhex(funder_hex.removeprefix("0x"))).public_key.to_checksum_address()

    op_hex = os.environ.get("PQ_OPERATOR_KEY", "")
    generated = not op_hex
    if generated:
        op_hex = "0x" + secrets.token_bytes(32).hex()
    operator = keys.PrivateKey(bytes.fromhex(op_hex.removeprefix("0x"))).public_key.to_checksum_address()

    endow = int(float(sys.argv[1]) * ETH) if len(sys.argv) > 1 else 5 * 10 ** 16
    print(f"funder {funder}  operator {operator}  endow {endow/ETH:.4f} ETH  chain {CHAIN_ID}")

    addr, rc = sponsor.deploy_sponsor(funder_hex, funder, operator, endow)
    ok = rc and rc.get("status") == "0x1"
    print(f"sponsor {addr}  status {rc.get('status') if rc else 'none'}  bal {rpc.get_balance(addr)/ETH:.4f} ETH")
    if not ok:
        return 1

    print("\n=== set these in the backend env, then restart ui/server.py ===")
    print(f"export PQ_SPONSOR={addr}")
    if generated:
        print(f"export PQ_OPERATOR_KEY={op_hex}    # GENERATED, SAVE IT (no funds, only authorizes)")
    else:
        print("export PQ_OPERATOR_KEY=<the one you passed>")
    print("\nTop up later by sending ETH to the sponsor address; it never holds user funds.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
