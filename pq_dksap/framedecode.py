"""Human-readable explanation of an EIP-8141 frame transaction (type 0x06),
built from the node's already-decoded `eth_getTransactionByHash` JSON. Also
handles the legacy funding transfer, so any tx hash from the demo can be
explained frame by frame.
"""

_MODE = {0: "DEFAULT", 1: "VERIFY", 2: "SENDER"}
_SCHEME = {0: "ARBITRARY", 1: "SECP256K1", 2: "P256"}
_SCOPE = {0: "none", 1: "APPROVE_PAYMENT", 2: "APPROVE_EXECUTION",
          3: "APPROVE_EXECUTION_AND_PAYMENT"}


def _int(x):
    return int(x, 16) if isinstance(x, str) else int(x or 0)


def _blen(hexstr):
    return (len(hexstr) - 2) // 2 if isinstance(hexstr, str) and hexstr.startswith("0x") else 0


def _role(frames, i):
    """Infer a frame's role and a plain-language note."""
    f = frames[i]
    mode = _int(f.get("mode"))
    scope = _int(f.get("flags")) & 0x3
    to = f.get("to")
    data = _blen(f.get("data"))
    val = _int(f.get("value"))
    if mode == 1:  # VERIFY
        return ("verify", f"Self-verify: the account authorizes this tx "
                f"({_SCOPE.get(scope, scope)}). Runs on-chain, gas-capped.")
    if mode == 2:  # SENDER
        return ("send", f"Moves {val/1e18:.6f} ETH to {to}.")
    # DEFAULT
    if to is not None and data > 0:
        return ("deploy", f"Deploy frame: calls the CREATE2 factory ({to}) with "
                f"salt||init_code ({data} B); it deploys the stealth account at the "
                f"sender address, so the following frames find code there.")
    return ("mldsa-verify", "The heavy ML-DSA verification runs here, in a DEFAULT "
            "frame with room for its ~4.8M gas. It reverts the whole transaction "
            "unless the inline post-quantum signature is valid.")


def _prefix_shape(frames):
    if not frames:
        return "unknown"
    m0 = _int(frames[0].get("mode"))
    if m0 == 0 and len(frames) > 1 and _int(frames[1].get("mode")) == 1:
        return "DeploySelfVerify (deploy + self-verify)"
    if m0 == 1:
        return "SelfVerify (self-verify)"
    return "unrecognized"


def explain(tx: dict) -> dict:
    """Turn an eth_getTransactionByHash result into a structured explanation."""
    if tx is None:
        return {"found": False}
    ttype = _int(tx.get("type"))

    if ttype != 6:
        # Legacy / typed non-frame transfer (the funding tx).
        return {
            "found": True, "kind": "legacy", "type": hex(ttype),
            "from": tx.get("from"), "to": tx.get("to"),
            "value_eth": _int(tx.get("value")) / 1e18,
            "input_bytes": _blen(tx.get("input")),
            "summary": ("A plain transfer: the payer funds the stealth address ahead of "
                        "its deploy. A classic ECDSA-signed transaction."),
        }

    frames = tx.get("frames", [])
    out_frames = []
    for i, f in enumerate(frames):
        role, note = _role(frames, i)
        out_frames.append({
            "index": i, "mode": _MODE.get(_int(f.get("mode")), "?"),
            "role": role, "scope": _SCOPE.get(_int(f.get("flags")) & 0x3),
            "to": f.get("to"), "value_eth": _int(f.get("value")) / 1e18,
            "exec_gas": _int(f.get("gasLimit")), "state_gas": _int(f.get("stateLimit")),
            "data_bytes": _blen(f.get("data")), "note": note,
        })

    sigs = []
    for s in tx.get("signatures", []):
        scheme = _int(s.get("scheme"))
        sig_b = _blen(s.get("signature"))
        note = ("Inline post-quantum credential: ML-DSA public key (22400 B) + "
                "signature (2420 B), fed straight to the on-chain verifier."
                if scheme == 0 and sig_b > 20000 else "")
        sigs.append({"scheme": _SCHEME.get(scheme, scheme),
                     "signer": s.get("signer"), "msg_bytes": _blen(s.get("msg")),
                     "signature_bytes": sig_b, "note": note})

    return {
        "found": True, "kind": "frame", "type": "0x06",
        "sender": tx.get("sender") or tx.get("from"),
        "nonce": _int(tx.get("nonceSeq")),
        "prefix_shape": _prefix_shape(frames),
        "frames": out_frames, "signatures": sigs,
        "summary": ("An EIP-8141 frame transaction: authentication happens as on-chain "
                    "execution. The prefix (deploy + verify) establishes who pays; the "
                    "value moves once the ML-DSA gate passes."),
    }
