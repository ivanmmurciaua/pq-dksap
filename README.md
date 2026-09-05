# pq-dksap

A runnable proof of concept: **a fully post-quantum stealth spend on-chain**, over [EIP-8141](https://eips.ethereum.org/EIPS/eip-8141) frame transactions, against the ethrex Hegota privacy testnet.

Funds sit at a stealth account and can be moved **only** by presenting a valid **ML-DSA** (Dilithium2) signature, which is verified **on-chain** by a shared verifier. No ECDSA anywhere in the spend path.

**Network:** ethrex Hegota privacy testnet — chain `8141` (`0x1fcd`), RPC `https://rpc1.privacy.ethrex.xyz`, explorer `https://dora.privacy.ethrex.xyz`.

Its EIP-8141 envelope also carries EIP-8250 (keyed nonces) and EIP-8272 (recent roots) fields; this PoC uses the linear nonce domain and references no recent roots.

## What it demonstrates

```
deploy a stealth account  ->  spend from it, authorized by an ML-DSA signature
                              verified on-chain, public key passed inline
```

A spend is a single frame transaction with three frames:

| frame | mode | role |
| --- | --- | --- |
| 0 | `VERIFY` | cheap self-approval (fits the 100k `MAX_VERIFY_GAS` prefix cap) |
| 1 | `DEFAULT` | the ~5M-gas ML-DSA verification — reverts the whole tx if the signature is invalid |
| 2 | `SENDER` | moves the value (only reached if frame 1 passed) |

The ML-DSA verification is too heavy for a `VERIFY` frame, so it runs in a `DEFAULT` frame and **revert-gates** the spend. The signature and the (large) public key travel **inline** in the transaction's `ARBITRARY` signature — no per-account key contract is deployed or stored.

### The verifier is a shared singleton

One `ZKNOX_ethdilithium` verifier (exposing `verifyInline(pk, m, sig)`) is deployed once and used by every stealth account, with each account's public key supplied in calldata. Storing the 22.4 KB key on-chain per account (`setKey` / SSTORE2) is a large one-time cost; passing it inline avoids that storage entirely — paid per spend, nothing persisted — which matters most for single-use stealth accounts.

Measured on the live testnet (single spend):

```
spend gas   ~5.33M   ( VERIFY 130 + DEFAULT/ML-DSA ~4.81M + SENDER 3k + state charges )
account deploy   ~0.52M   (one-time)
```

## Setup

Requires Python 3.10+.

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt

# ML-DSA ETH-variant signer (ZKNox reference, not on PyPI):
pip install "polyntt @ git+https://github.com/ZKNoxHQ/NTT.git@main#subdirectory=assets/pythonref/"
pip install --no-deps "git+https://github.com/ZKNoxHQ/ETHDILITHIUM.git#subdirectory=pythonref"
```

You need a normal secp256k1 EOA with test ETH — it is used **only** to deploy and endow the stealth account (the spend itself is ML-DSA). Get test ETH at <https://faucet.privacy.ethrex.xyz/>.

## Run

The funder key is "Alice", the payer. Everything else — Bob's wallet and Bob's stealth key — is generated fresh each run; nothing is hardcoded.

```bash
export PQ_FUNDER_KEY=0x<your-test-eth-key>      # or put it in ./.funder_key ; this is Alice
python demo.py                                  # Alice pays Bob 0.0001 ETH via a stealth address
python demo.py --amount 500000000000000         # pay a different amount (wei)
python demo.py --seed <64-hex>                  # pin Bob's stealth key
```

The output tells the story Alice -> Bob's stealth address -> Bob's wallet.

A testnet can stall or reset without notice, so a run saves its state to `./.pqstate.json` (gitignored). If the sweep does not confirm in time, resume it once the chain advances — no redeploy, no new transaction:

```bash
python demo.py --resume
```

## Layout

```
demo.py                 end-to-end console demo
pq_dksap/
  config.py             network, singleton address, sizes
  rpc.py                JSON-RPC + faucet
  frametx.py            EIP-8141 (type 0x06) encoding + canonical sig_hash
  legacytx.py           legacy tx signing (for deploys)
  mldsa.py              ML-DSA-44 ETH-variant keygen / expanded pk / sign
  stealth.py            deploy account, build + authorize a spend
contracts/
  Account.yul           the stealth account (ethrex Hegota dialect)
  Account.bin           compiled creation bytecode (commit passed as constructor arg)
```

To recompile the account (only if you change `Account.yul`):

```bash
solc-select use 0.8.23
solc --strict-assembly --optimize --bin contracts/Account.yul   # take the hex into Account.bin
```

## What this is — and isn't (vs classic DKSAP)

This repo is the **on-chain spend engine** of a post-quantum stealth-payment system: funds gated by an ML-DSA signature verified on-chain. That engine, on its own, is trustless and permissionless. It is **not yet a full DKSAP** — the protocol layer that makes a payment non-interactive, unlinkable, and discoverable is not implemented here. Piece by piece:

| DKSAP piece | Classic (ERC-5564) | This PoC |
| --- | --- | --- |
| Spend authorization | ECDSA on the stealth key | **ML-DSA, verified on-chain** |
| Key agreement / derivation | ECDH tweak: the payer derives the address, only the recipient can spend | **not implemented** — the recipient generates the key from a seed (would be *blinded ML-DSA* over an ML-KEM shared secret) |
| Meta-address registry (ERC-6538) | recipient publishes a meta-address | not implemented |
| Announcer (ERC-5564) | payer publishes an announcement to be scanned | not implemented |
| Recipient scanning | detect payments with a viewing key | not implemented |
| Account deployment | counterfactual, canonical | payer deploys via `CREATE` (should be `CREATE2` from a canonical factory) |
| Gas | often sponsored | account self-pays (should be sponsored, so the stealth holds zero ETH) |

**Consequence:** here the recipient generates the stealth key and the payer is handed the address (an interactive shortcut), so this demonstrates the **spend mechanism**, not the unlinkable non-interactive payment. The missing key-agreement layer (*blinded ML-DSA* over ML-KEM: the payer computes the address, only the recipient can spend, and the two are unlinkable) is real work that already exists in the pq-sap project (see Acknowledgments) and plugs on top of this exact machinery without changing it.

Testnet only. Not audited. Not for production.

## Acknowledgments

This PoC stands on other people's work:

- **Post-quantum ERC-5564 stealth addresses / blinded ML-DSA** — the DKSAP design this on-chain machinery is meant to carry (the payer computes the address, only the recipient can spend, via additive blinding of the MLWE key) comes from the pq-sap project by Skas ([Skanislav/pq-sap](https://github.com/Skanislav/pq-sap)).
- **ETHDILITHIUM & NTT** — the on-chain ML-DSA verifier and the reference signer are ZKNox's ([ZKNoxHQ/ETHDILITHIUM](https://github.com/ZKNoxHQ/ETHDILITHIUM), [ZKNoxHQ/NTT](https://github.com/ZKNoxHQ/NTT)).
- **EIP-8141 frame transactions** — the account-abstraction substrate, and the ethrex Hegota privacy testnet ([lambdaclass/ethrex](https://github.com/lambdaclass/ethrex)) it runs on.
- **ML-DSA (FIPS-204)** and **ML-KEM (FIPS-203)** — the NIST post-quantum standards.
