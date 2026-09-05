"""Stealth account lifecycle: derive a key, deploy the account, build and sign a
post-quantum spend over EIP-8141 frame transactions."""
import time
from dataclasses import dataclass

from eth_utils import keccak

from . import mldsa, rpc
from .config import CHAIN_ID, GAS_PRICE, account_creation_code
from .frametx import (
    Frame, FrameTx, Signature, MODE_DEFAULT, MODE_SENDER, MODE_VERIFY,
    FLAG_APPROVE_EXECUTION_AND_PAYMENT, FLAG_NONE, SCHEME_ARBITRARY,
)
from .legacytx import create_address, sign_legacy


@dataclass
class StealthKey:
    seed: bytes
    pk: object
    sk: object
    pk_deploy: bytes
    commit: bytes   # keccak256(pk_deploy) — the account's key commitment

    @classmethod
    def from_seed(cls, seed: bytes) -> "StealthKey":
        pk, sk = mldsa.keypair(seed)
        pk_deploy = mldsa.expanded_pk(pk)
        return cls(seed, pk, sk, pk_deploy, keccak(pk_deploy))


def deploy_account(funder_hex: str, funder: str, key: StealthKey, fund_wei: int):
    """Deploy the stealth account (endowed with fund_wei) and return its address.

    The account is created with its key commitment appended as the constructor
    argument, so a single compiled artifact serves any stealth key. Funding via
    CREATE endows the account without running its runtime.
    """
    init = account_creation_code() + key.commit
    call = {"from": funder, "data": "0x" + init.hex(), "value": hex(fund_wei)}
    gas = rpc.estimate_gas(call)
    nonce = rpc.get_nonce(funder)
    addr = create_address(funder, nonce)
    raw, _ = sign_legacy(funder_hex, nonce=nonce, gas_price=GAS_PRICE, gas_limit=gas + 100_000,
                         to=None, value=fund_wei, data=init, chain_id=CHAIN_ID)
    txh = rpc.send_raw(raw)
    receipt = _wait(txh)
    return addr, receipt


def build_spend(account: str, dest: str, value: int) -> FrameTx:
    """Build the unsigned spend: [VERIFY(cheap approve), DEFAULT(verify gate),
    SENDER(move value)] with an empty ARBITRARY signature placeholder."""
    return FrameTx(
        chain_id=CHAIN_ID, nonce=rpc.get_nonce(account), sender=account,
        frames=[
            # DEFAULT execution_gas sized to the ~4.8M ML-DSA verify + margin.
            # max_gas (sum of frame limits) sets the up-front max-cost the payer
            # must hold; kept small so the account needs minimal ETH parked.
            Frame(MODE_VERIFY, FLAG_APPROVE_EXECUTION_AND_PAYMENT, None, 50_000, 45_000, 0, b""),
            Frame(MODE_DEFAULT, FLAG_NONE, None, 8_000_000, 300_000, 0, b""),
            # SENDER state_gas must cover the EIP-8037 NEW_ACCOUNT charge when the
            # recipient is a fresh address (~200k+ on ethrex); too low reverts.
            Frame(MODE_SENDER, FLAG_NONE, dest, 100_000, 500_000, value, b""),
        ],
        signatures=[Signature(SCHEME_ARBITRARY, None, b"", b"")],
        max_priority_fee_per_gas=10 ** 8, max_fee_per_gas=2 * 10 ** 8,
    )


def authorize(tx: FrameTx, key: StealthKey) -> bytes:
    """Sign the tx's canonical sig_hash with ML-DSA and place pk||sig inline in
    the ARBITRARY signature. Returns the sig_hash that was signed."""
    h = tx.sig_hash()
    sig = mldsa.sign(key.sk, h)
    assert mldsa.verify(key.pk, h, sig), "local ML-DSA verify failed"
    tx.signatures[0].signature = key.pk_deploy + sig
    return h


def _wait(txhash, timeout=1000):
    t0 = time.time()
    last = 0
    while time.time() - t0 < timeout:
        r = rpc.get_receipt(txhash)
        if r:
            return r
        el = int(time.time() - t0)
        if el - last >= 15:
            print(f"      ... still waiting for confirmation ({el}s; this testnet mines in "
                  f"bursts and can stall for minutes)", flush=True)
            last = el
        time.sleep(3)
    return None
