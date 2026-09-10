# Security notes

This is a **proof of concept**, not audited, testnet only. This file records the security model it actually achieves and the known gaps between that and the model it is meant to achieve. Read it before drawing any conclusion about what the post-quantum guarantee here does and does not cover.

## Threat-model summary (the honest version)

An amount of ETH passes through these custody phases:

```
payer's k1 EOA   ->   stealth account   ->   recipient's payout
  (classical)          (see Finding 1)        PQ smart wallet (post-quantum)
                                              or a k1 EOA (classical, Finding 2)
```

The funding endpoint is an ordinary secp256k1 account (classical). The stealth account in the middle is meant to be post-quantum, and Finding 1 qualifies exactly how. The payout is where this PoC now improves on the classical default: the UI sweeps into a persistent **PQ smart wallet** whose withdraw is ML-DSA hard-gated (see "The persistent PQ smart wallet" below), so custody stays post-quantum. Cashing out from there to a k1 EOA is a deliberate, explicit return to classical security (Finding 2).

## Finding 1, the ML-DSA signature is not enforced by the account

**What the design intends:** funds at the stealth account can be moved *only* by presenting a valid ML-DSA signature, verified onchain.

**What the code actually enforces:** the ML-DSA signature is *not* a hard gate.

### Mechanism

Under EIP-8141 (ethrex levm), a `SENDER` frame moves value only when `sender_approved` is true, and `sender_approved` is set by the `APPROVE` opcode.

`APPROVE` is **unconditional**: it checks only that the approving contract is `tx.sender`; it verifies no signature.

To fit the 100k `MAX_VERIFY_GAS` cap on the validation prefix, this account splits the work across two frames:

- a cheap `VERIFY` frame that calls `APPROVE` (unconditionally), and
- a separate `DEFAULT` frame that runs the ~5M-gas ML-DSA verification and reverts the whole transaction if the signature is invalid.

The heavy verify only gates the spend **when that frame is present**. A transaction of the shape `[deploy, VERIFY(approve), SENDER -> attacker]`, omitting the ML-DSA frame, sets `sender_approved` and moves the value **without any ML-DSA signature**.

### What actually protects the funds today

Not the ML-DSA signature: **knowing the account's `commit`** (`keccak256` of the expanded blinded public key). To run `APPROVE` the account must first be deployed, and deploying it (via the CREATE2 factory) requires the `commit`, because the account's CREATE2 address binds to it.

- **Third parties (classical or quantum): cannot steal.** The `commit` is not recoverable from the onchain address (a keccak preimage), and not recoverable from a published ML-KEM ciphertext without the recipient's ML-KEM decapsulation key. Deriving the `commit` reduces to breaking ML-KEM. So the custody against the outside world does hold, and holds post-quantum, but via **ML-KEM secrecy of the `commit`**, not via the ML-DSA signature.
- **The payer can reverse the payment.** Whoever derived the stealth address knows the `commit` (the payer computes it to fund the address). Before the recipient sweeps, the payer could deploy the account and drain it back with an approve-only transaction, no ML-DSA needed. This is not theft of the recipient's funds (it is the payer un-sending their own money), but it breaks **payment finality**: the recipient cannot rely on the funds until they have swept them.

### Severity

Not "funds are open to anyone." The realistic impact is:

1. the enforced authorization is `commit`-knowledge (ML-KEM-backed), not the ML-DSA signature, a mismatch with the intended, signature-based model; and
2. a payer-side payment-reversal / finality gap, mitigated in practice by the recipient sweeping promptly.

### Fix

Make `APPROVE` conditional on a valid ML-DSA verification, which requires the verification to fit inside the 100k validation-prefix cap. The ~5M-gas native verify does not fit. Two paths make it cheap enough, either fitting approval and verification in one frame:

- A **succinct (SNARK) proof** of the verification, small enough to check in the prefix; or
- A **native ML-DSA precompile** ([EIP-8355](https://eips.ethereum.org/EIPS/eip-8355), roughly 6.5k gas for a 32-byte message), which is **not yet deployed** on this testnet (checked against the running node and the ethrex source).

Either turns "commit-gated" into genuinely "ML-DSA-gated" and closes the payer-reversal vector (no value moves without the signature). Note that the persistent **PQ smart wallet already side-steps this finding entirely**: it verifies ML-DSA in normal, uncapped execution, so its withdraw is signature-gated today. Finding 1 is specific to the gas-capped self-paying stealth account.

## Finding 2, cashing out to a k1 EOA is classical

If a sweep or a withdraw targets an ordinary secp256k1 EOA, once funds land there they are back under classical security: an attacker with that key (or a quantum break of secp256k1) can take them. This is now the **deliberate exit**, not the default: the UI sweeps into the PQ smart wallet below, which keeps custody post-quantum, and only a withdraw to a chosen wallet returns to classical. Every classical touch, funding from a k1 wallet, or cashing out to one, is an explicit classical exposure.

## The persistent PQ smart wallet: custody that hard-enforces ML-DSA

Unlike the one-shot stealth account (Finding 1), the PQ smart wallet is an ordinary contract whose `withdraw` verifies the ML-DSA signature in **normal, uncapped execution**. There is no 100k prefix cap to dodge, so the signature is a **hard gate**: a plain ECDSA withdraw with no valid ML-DSA signature reverts, and whoever submits the transaction cannot steal or redirect it (the signed message binds `dest, amount, nonce, wallet, chainid`). Finding 1 does **not** apply here; this is where persistent funds stay post-quantum.

### Its own caveat, the key source

The wallet's ML-DSA key must come from a post-quantum-safe secret, or the custody inherits a classical weakness through the back door:

- **Passkey PRF (default when available).** The key derives from a WebAuthn PRF secret held in the authenticator's secure element, not recoverable from any public key. Breaking or stealing the secp256k1 wallet does not yield it. Post-quantum at the root.
- **Wallet signature + PIN (fallback).** The key is `scrypt(PIN, wallet-signature)`. A stolen or quantum-broken k1 key alone is not enough: the attacker also needs the PIN, and scrypt makes guessing it against the public wallet address expensive. Its post-quantum strength is exactly the **PIN entropy**, so use a strong passphrase, not a short PIN.

## Gasless sponsorship, trust model

Vault deploy and withdraw can be paid by a sponsor contract, gated by an operator signature (see the README). The trust boundary is deliberately tight:

- **Cannot steal or redirect.** The ML-DSA signature inside the withdraw fixes destination and amount; the sponsor is only the transaction sender paying gas.
- **Cannot be griefed.** An operator signature that does not `ecrecover` to the configured operator is rejected in the validation prefix, before the transaction mines, so no gas is burned on unauthorized calls.
- **The operator key holds no funds.** It only authorizes which transactions the sponsor pays. If it leaks, the blast radius is the sponsor contract's gas balance (an attacker can drain it on sponsored calls), never a vault (ML-DSA gated) and never user funds. Rotate by deploying a fresh sponsor.
- **Residual trust is liveness, not custody.** A malicious or offline operator can refuse to sponsor, but cannot move funds; the user can always pay their own gas.

## Standing caveats

- Testnet only, not audited, not for production.
- Gas parameters are tuned empirically against a live testnet and are not a consensus-safe configuration.
- The blinded-ML-DSA construction (see the README acknowledgments) is research-grade and unaudited.
