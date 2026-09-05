"""Minimal JSON-RPC client + faucet for the ethrex Hegota privacy testnet."""
import json

import requests

from .config import FAUCET_URL, RPC_URL

class RpcError(Exception):
    pass

_id = 0

def rpc(method, params=None, url=RPC_URL):
    global _id
    _id += 1
    r = requests.post(
        url,
        json={"jsonrpc": "2.0", "id": _id, "method": method, "params": params or []},
        timeout=30,
    )
    r.raise_for_status()
    data = r.json()
    if "error" in data:
        raise RpcError(json.dumps(data["error"]))
    return data["result"]


def to_int(hexstr):
    return int(hexstr, 16)


def get_balance(addr):
    return to_int(rpc("eth_getBalance", [addr, "latest"]))


def get_nonce(addr):
    return to_int(rpc("eth_getTransactionCount", [addr, "latest"]))


def get_code(addr):
    return rpc("eth_getCode", [addr, "latest"])


def get_receipt(txhash):
    return rpc("eth_getTransactionReceipt", [txhash])


def send_raw(raw_hex):
    return rpc("eth_sendRawTransaction", [raw_hex])


def estimate_gas(call):
    return to_int(rpc("eth_estimateGas", [call]))


def eth_call(call, block="latest"):
    return rpc("eth_call", [call, block])


def faucet_claim(addr):
    r = requests.post(FAUCET_URL, json={"address": addr}, timeout=60)
    try:
        return r.status_code, r.json()
    except ValueError:
        return r.status_code, r.text
