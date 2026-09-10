# pq-dksap

**A fully post-quantum stealth spend onchain** runnable PoC, over [EIP-8141](https://eips.ethereum.org/EIPS/eip-8141) frame transactions, against the ethrex Hegota privacy testnet.

Funds sit at a stealth account and can be moved **only** by presenting a valid **ML-DSA** (Dilithium2) signature, which is verified **onchain** by a shared verifier. For funds that persist, the same ML-DSA gate backs a reusable **PQ smart wallet**, and both its deploy and its withdraw are **gasless** for the recipient through a native EIP-8141 sponsor.

**Network:** ethrex Hegota privacy testnet, chain `8141` (`0x1fcd`), RPC `https://rpc1.privacy.ethrex.xyz`, explorer `https://dora.privacy.ethrex.xyz`.

Its EIP-8141 envelope also carries EIP-8250 (keyed nonces) and EIP-8272 (recent roots) fields; this PoC uses the linear nonce domain and references no recent roots.

## What it demonstrates

```
deploy a stealth account  ->  spend from it, authorized by an ML-DSA signature
                              verified onchain, public key passed inline
```

A spend is a single frame transaction with three frames:

| frame | mode | role |
| --- | --- | --- |
| 0 | `VERIFY` | cheap self-approval (fits the 100k `MAX_VERIFY_GAS` prefix cap) |
| 1 | `DEFAULT` | the ~5M-gas ML-DSA verification, reverts the whole tx if the signature is invalid |
| 2 | `SENDER` | moves the value (only reached if frame 1 passed) |

The ML-DSA verification is too heavy for a `VERIFY` frame, so it runs in a `DEFAULT` frame and **revert-gates** the spend. The signature and the (large) public key travel **inline** in the transaction's `ARBITRARY` signature, no per-account key contract is deployed or stored.

### The verifier is a shared singleton

One `ZKNOX_ethdilithium` verifier (exposing `verifyInline(pk, m, sig)`) is deployed once and used by every stealth account, with each account's public key supplied in calldata. Storing the 22.4 KB key onchain per account (`setKey` / SSTORE2) is a large one-time cost; passing it inline avoids that storage entirely, paid per spend, nothing persisted, which matters most for single-use stealth accounts.

Measured on the live testnet (single spend):

```
spend gas   ~5.33M   ( VERIFY 130 + DEFAULT/ML-DSA ~4.81M + SENDER 3k + state charges )
account deploy   ~0.52M   (one-time)
```

## Persistent post-quantum custody: the PQ smart wallet

The one-shot stealth account above **self-pays** its gas, which (see [SECURITY.md](SECURITY.md)) means it approves payment without *hard-enforcing* the ML-DSA signature so the effective gate is knowledge of the key commitment, protected by ML-KEM. That is fine for a single-use sweep, not for funds that persist.

For funds that stay put, the local UI (`ui/`) sweeps into a **PQ smart wallet** that it is an ordinary contract (`contracts/Vault.sol`) whose `withdraw` verifies the ML-DSA signature in **normal, uncapped execution**, so the signature is a **hard gate**. A plain ECDSA withdraw with no valid ML-DSA signature reverts. The signed message binds `(dest, amount, nonce, wallet, chainid)`, so whoever submits the transaction cannot steal or redirect the funds.

Its ML-DSA key comes from a **WebAuthn passkey PRF secret** (held in the authenticator's secure element, not recoverable from any public key), or, on devices without PRF, from a wallet signature **plus a user PIN** stretched through scrypt. A stolen or quantum-broken secp256k1 key alone therefore **cannot reach the funds**.

The wallet is **counterfactual** that means its CREATE2 address is derived up front and can receive funds before it exists onchain and it is deployed only when the sweep lands in it.

## Gasless: native EIP-8141 sponsorship

Neither the wallet deploy nor the withdraw needs the recipient to hold any ETH. A **sponsor contract** (`contracts/Sponsor.yul`) is the frame-transaction sender and its `VERIFY` frame approves payment only after an `ecrecover` of an **operator** signature over the transaction's `sig_hash`, so the sponsor pays the gas. On the live testnet a sponsored deploy + withdraw costs the sponsor about **~0.000143 ETH** total.

- It **cannot steal**: the ML-DSA signature inside the withdraw fixes destination and amount.
- It **cannot be griefed**: a wrong operator signature is rejected in the validation prefix, before the transaction mines.
- The operator key **holds no funds**, it only signs, the gas comes from the sponsor contract's balance.

This is a bundler + paymaster in the ERC-4337 sense, but native to EIP-8141 (no EntryPoint, no UserOperations) and run by a single operator. See `scripts/deploy_sponsor.py` and `scripts/poc_sponsor*.py`.

## Forward: native ML-DSA precompiles (EIP-8355)

The ~5M-gas ML-DSA verification is essentially the entire cost of a spend, and it is what does **not** fit the 100k `VERIFY` prefix, which is why the one-shot stealth account cannot hard-enforce its own signature. [EIP-8355](https://eips.ethereum.org/EIPS/eip-8355) proposes native ML-DSA precompiles (`0x12` for ML-DSA-44) at `6500 + 6 per message word`, roughly **6.5k gas** for a 32-byte message: a ~758x reduction. It is **not deployed** on the Hegota testnet today. If it lands, the verify would fit the prefix and even the one-shot self-pay could hard-enforce the signature and reusing it would mean switching from the Keccak profile (ZKNox) to standard FIPS-204 (SHAKE) ML-DSA.

## Setup

Requires Python 3.10+.

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt

# ML-DSA ETH-variant signer (ZKNox reference, not on PyPI):
pip install "polyntt @ git+https://github.com/ZKNoxHQ/NTT.git@main#subdirectory=assets/pythonref/"
pip install --no-deps "git+https://github.com/ZKNoxHQ/ETHDILITHIUM.git#subdirectory=pythonref"
```

You need a normal secp256k1 EOA with test ETH, it is used **only** to deploy and endow the stealth account (the spend itself is ML-DSA). Get test ETH at <https://faucet.privacy.ethrex.xyz/>.

## Run

### Console demo

The funder key is "Alice", the payer. Everything else, Bob's wallet and Bob's stealth key, is generated fresh each run.

```bash
export PQ_FUNDER_KEY=0x<your-test-eth-key>      # or put it in ./.funder_key ; this is Alice
python demo.py                                  # Alice pays Bob 0.0001 ETH via a stealth address
python demo.py auto --amount 500000000000000    # pay a different amount (wei)
python demo.py auto --seed <64-hex>             # pin Bob's stealth key
```

The output tells the story Alice -> Bob's stealth address -> Bob's wallet.

A testnet can stall or reset without notice, so a run saves its state to `./.pqstate.json` (gitignored). If the sweep does not confirm in time, **re-run the same command** once the chain advances, it picks up the queued sweep from the state file and finishes it, no redeploy, no new transaction.

### Interactive UI (stealth pay, PQ smart wallet, gasless)

The local UI (`ui/server.py`, a standard-library backend plus a thin browser client) drives the full flow: a stealth payment swept into a persistent PQ smart wallet, then withdrawn. Open it at <http://localhost:8000>.

Each piece below is **optional and independent**:

```bash
export PQ_FACTORY=0x<create2-factory>       # from: python demo.py factory-deploy
python ui/server.py
```

- **Alice is optional** Without `PQ_FUNDER_KEY`, the UI runs in *external-payment mode*, there is no payer key on the server, and you fund the one-time stealth address from any wallet by scanning its QR. Set `PQ_FUNDER_KEY` only for the in-app "Pay" convenience. The whole PoC is fully runnable with no Alice.
- **Gasless is optional** Set `PQ_SPONSOR` and `PQ_OPERATOR_KEY` and the smart wallet's deploy and withdraw are paid by the sponsor, so the recipient holds zero ETH. Without them, the connected wallet pays that gas. Alice and the sponsor are unrelated so you can hide Alice's flow and still be gasless, or the reverse.

To set up the sponsor once (prints `PQ_SPONSOR`, and a generated `PQ_OPERATOR_KEY` if you did not pass one):

```bash
export PQ_FUNDER_KEY=0x<funded-key>         # pays the deploy and endows the sponsor
python scripts/deploy_sponsor.py 0.05       # endow it with 0.05 ETH
```

The **operator key holds no funds**: it only signs which transactions the sponsor pays. The gas comes from the sponsor contract's balance (top it up by sending ETH to its address). If the operator key is ever leaked, an attacker can at most drain the sponsor's gas, never touch a vault (those are ML-DSA gated).

Note that `PQ_FUNDER_KEY` does double duty: a funded key for the one-time setup below, and, if it is still set when you run the UI, Alice's "Pay" button. So for a no-Alice run you must `unset` it before starting the server. 

A typical full gasless setup from scratch:

```bash
# One-time setup: needs a funded EOA to pay for the two deploys.
export PQ_FUNDER_KEY=0x<funded-key>
python demo.py factory-deploy               # deploy the CREATE2 factory, note its address
python scripts/deploy_sponsor.py 0.05       # deploy + endow the sponsor, note PQ_SPONSOR and PQ_OPERATOR_KEY

# Run the UI, fully gasless.
export PQ_FACTORY=0x<factory-from-above>
export PQ_SPONSOR=0x<sponsor-from-above>
export PQ_OPERATOR_KEY=0x<operator-from-above>
unset PQ_FUNDER_KEY                          # no Alice: external-payment mode (still gasless)
python ui/server.py                         # http://localhost:8000 ; deploy + withdraw fully gasless

# Keep PQ_FUNDER_KEY set instead of unsetting it to also enable Alice's in-app "Pay" button.
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
  dksap.py              blinded ML-DSA over ML-KEM (payer derives, recipient spends)
  stealth.py            deploy account, build + authorize a self-paid spend
  vault.py              the PQ smart wallet: key derivation, deploy, withdraw
  sponsor.py            native EIP-8141 gasless sponsorship (operator-gated)
  framedecode.py        human-readable explanation of a frame transaction
contracts/
  Account.yul / .bin    the one-shot stealth account
  Factory.yul / .bin    minimal CREATE2 factory (counterfactual deploys)
  Vault.sol / .bin      the PQ smart wallet (ML-DSA-gated withdraw)
  Sponsor.yul / .bin    the operator-gated gasless sponsor
ui/                     local backend + thin browser client (server.py + js/)
scripts/                one-time deploys + onchain sponsor PoCs
```

To recompile the account (only if you change `Account.yul`):

```bash
solc-select use 0.8.23
solc --strict-assembly --optimize --bin contracts/Account.yul   # take the hex into Account.bin
```

## What this is, and isn't (vs classic DKSAP)

This repo is the **onchain spend engine** of a post-quantum stealth-payment system. Funds are gated by an ML-DSA signature verified onchain, that engine, on its own, is trustless and permissionless. It is **not yet a full DKSAP**, the protocol layer that makes a payment non-interactive, unlinkable, and discoverable is not implemented here. Piece by piece:

| DKSAP piece | Classic (ERC-5564) | This PoC |
| --- | --- | --- |
| Spend authorization | ECDSA on the stealth key | **ML-DSA, verified onchain** |
| Key agreement / derivation | ECDH tweak: the payer derives the address, only the recipient can spend | **blinded ML-DSA over an ML-KEM shared secret**: the payer derives the address from public data, only the recipient can spend, unlinkable |
| Meta-address registry (ERC-6538) | recipient publishes a meta-address | not implemented |
| Announcer (ERC-5564) | payer publishes an announcement to be scanned | **not implemented** (the ML-KEM ciphertext is delivered offchain) |
| Recipient scanning | detect payments with a viewing key | not implemented |
| Account deployment | counterfactual, canonical | **counterfactual `CREATE2`** via a shared factory |
| Gas | often sponsored | self-paid for the one-shot sweep; **sponsored** (zero recipient ETH) for the persistent smart wallet |

**Consequence:** the key-agreement layer (*blinded ML-DSA* over ML-KEM: the payer computes the address from public data, only the recipient can spend, and the two are unlinkable) is now implemented (`dksap.py`), so the demo shows the unlinkable non-interactive derivation as well as the spend. What is still missing is the **discovery** layer: an onchain registry, announcer, and scanning. Today the ML-KEM ciphertext that lets the recipient recover their key is delivered offchain, so a payment is spendable but not yet *discoverable* without that side channel. That layer is real, deliberate future work, not discarded.

### Recoverability

Funds sit at the stealth account (a contract), not at a "Bob address". To move them Bob must produce a blinded ML-DSA signature the onchain verifier accepts, which needs **two** inputs:

- **Bob's master seed**, the one secret behind `(s1, s2, kem_dk)`, shared across every payment; `bob-sweep` reconstructs the spending material from it.
- **That payment's ML-KEM ciphertext** (the announcement), without it Bob cannot recover the shared secret, so he cannot form the blinded key for that specific stealth account.

So the value is fully recoverable from `seed + ciphertext`. This PoC delivers the ciphertext **offchain**. If Bob loses it and there is no onchain record, the funds are **stuck**, the seed alone is not enough. Making the ciphertext durable (Bob rescans and recovers with his seed only) is exactly what the onchain Announcer / registry / scanning layer provides. It is left as future work, not discarded. The sweep lands at Bob's payout wallet, whose key `bob-init` stores in the private secret file so Bob controls it.

Testnet only. Not audited. Not for production. See [SECURITY.md](SECURITY.md) for the security model this actually achieves and its known gaps, notably that the account does not yet hard-enforce the ML-DSA signature.

## Acknowledgments

This PoC stands on other people's work:

- **Post-quantum ERC-5564 stealth addresses / blinded ML-DSA**, the DKSAP design this onchain machinery is meant to carry (the payer computes the address, only the recipient can spend, via additive blinding of the MLWE key) comes from the pq-sap project by Skas ([Skanislav/pq-sap](https://github.com/Skanislav/pq-sap)).

- **ETHDILITHIUM & NTT**, the onchain ML-DSA verifier and the reference signer are ZKNox's ([ZKNoxHQ/ETHDILITHIUM](https://github.com/ZKNoxHQ/ETHDILITHIUM), [ZKNoxHQ/NTT](https://github.com/ZKNoxHQ/NTT)).

- **EIP-8141 frame transactions**, the account-abstraction substrate, and the ethrex Hegota privacy testnet ([lambdaclass/ethrex](https://github.com/lambdaclass/ethrex)) it runs on.

- **ML-DSA (FIPS-204)** and **ML-KEM (FIPS-203)**, the NIST post-quantum standards.
