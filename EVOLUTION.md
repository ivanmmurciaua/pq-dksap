# Natural evolution as EIP-8141 matures

This PoC works around the *current* state of frame transactions: a hard `MAX_VERIFY_GAS` cap, no post-quantum precompile, no signature aggregation, and a bare value-transfer for the sweep. Several of those workarounds are temporary bc the frame-transaction design already anticipates native mechanisms that dissolve them. This file maps each of today's limitations to how it is expected to resolve, following lightclient's "Frames Are All You Need" (<https://lightclient.io/blog/frames-are-all-you-need/>).

The strategic takeaway from that post: *"Frames aren't the last change we'll make to the EVM, but they should be the last transaction type we need for accounts."* Building on frames (rather than ERC-4337 / EIP-7702) is the durable bet, and the pieces we hand-rolled are placeholders for enshrined ones.

## 1. The signature is not hard-enforced (see SECURITY.md, Finding 1) -> `SIGNATURE` frames + aggregation

Today the ML-DSA verify (~5M gas, a 24.8 KB inline public key + signature) does not fit the ~100k validation-prefix cap, so `APPROVE` cannot be made conditional on it, and authorization is effectively `commit`-gated rather than signature-gated.

The post names the fix directly. Post-quantum crypto is *"big. Like, signatures that are two to ten kilobytes big,"* and a naive account pays *"half a million gas for their signature."* The answer is a dedicated **`SIGNATURE` frame mode** that targets *"canonical cryptographic contracts, likely precompiles,"* with the signature data *"stripped"* and replaced by a compressed witness (aggregation, in the spirit of EIP-8288), verified via focused techniques (*"LeanVM"*) until a general zkEVM exists.

When that lands, the ML-DSA verification becomes cheap enough to run *inside* the validation prefix, so approval and verification collapse into one step and the signature becomes the enforced gate. Our split (cheap unconditional `APPROVE` + a separate heavy verify that can be omitted) exists **only** because that native path is not here yet.

The concrete precompile now exists as a proposal: [EIP-8355](https://eips.ethereum.org/EIPS/eip-8355) adds native ML-DSA verifiers at `0x12`/`0x13`/`0x14`, costing roughly **6.5k gas** for a 32-byte message (about **758x** cheaper than the ~5M-gas contract verify). It is **not yet deployed** on this testnet (checked against the running node and the ethrex source). If it lands, `MAX_VERIFY_GAS` stops mattering for ML-DSA and Finding 1 closes even for the self-paying stealth account. Reusing it means moving from the Keccak profile (ZKNox) to standard FIPS-204 (SHAKE) ML-DSA. The persistent PQ smart wallet does not wait for it: it already verifies in normal, uncapped execution.

## 2. The residue left in the stealth -> the `SWEEP` instruction

Today a self-paid spend must retain a gas reserve (`max_cost`) up front and can only send `balance - reserve` to the payout, stranding `max_cost - gasUsed`.

The post introduces a **`SWEEP`** instruction that *"sends all funds to a destination and changes the account that is due to receive the refund."* That is exactly our reserve-and-drain, enshrined: the whole balance goes to the payout and the gas refund is redirected there too, so the residue goes to ~0 with no manual reserve arithmetic. Our dynamic-fee reserve logic is a stand-in for it.

## 3. Big inline credentials -> stripping + aggregation

The 24.8 KB inline `pk || sig` is the dominant calldata and verify cost. Under aggregation the signature is *"stripped"* and replaced with a compressed witness, and *"frame data remains never introspectable by another frame"* precisely to enable that future elision. Many verifications can then be aggregated into one, which is the real lever for making per-payment PQ spends cheap.

## 4. L1 vs L2 rollout: static determinability

The post is explicit that this is *"a very layer-one-centric view of transacting."* Other chains face a tradeoff: *"until a user can construct a zk proof that their transaction is valid against some prestate, arbitrary validation always needs to be executed."* Chains that want a unified, statically-reasonable UX may offer *"a prix-fixe menu"* instead of full programmability, via keystores, required known wallet implementations, or protocol-enforced algorithm lists.

Design implication for anything built on this: **do not assume every chain allows arbitrary onchain validation.** On a restrictive L2 the ML-DSA account may need to be a *known* implementation (keystore / whitelisted verifier) rather than an arbitrary contract, or ship a zk validity proof. The verifier-slot approach here is compatible with either, but the deployment story differs per chain.

## 5. Crypto-agility is the endgame

The design goal is *"freedom of choice and not be constrained by a prix-fixe menu"*, building blocks combinable in *"arbitrary ways, such as a multisig"* over *"heterogeneous sets of cryptographic algorithms."* A stealth account gated by ML-DSA is one point in that space; the same substrate later carries hybrid (classical + PQ) or aggregated multi-scheme authorization without protocol branching.

## 6. Gasless sponsorship: an operator-gated sponsor today, native payment tomorrow

Today the recipient pays no gas: a sponsor contract is the frame-transaction sender, approves payment (EIP-8141's native payment approval, where `payer = frame.target`), and pays. We gate it with a **per-transaction operator ECDSA signature** (an `ecrecover` in the prefix), because the prefix cannot cheaply verify anything richer, the same Finding 1 root.

Two directions mature this. First, the protocol is already adding first-class sponsorship: EIP-8312's UTXO frames expose a `vault_sender` waiver that lets one sponsor serve many spends natively (though for classical UTXOs, not our PQ contract vault). Payment frames plus that waiver are the native paymaster. Second, cheaper in-prefix verification (item 1, e.g. EIP-8355) would let the sponsor gate on richer policy, or let the account self-sponsor gated by its real signature, dropping the separate operator. The endgame is a permissionless relayer/paymaster market rather than a single operator; our operator-gated sponsor is the minimal working stand-in.

## Summary table

| Today's workaround here | Enshrined replacement (per the post) |
| --- | --- |
| Split cheap-approve / heavy-verify (Finding 1) | `SIGNATURE` frame + PQ precompile (EIP-8355) / aggregation |
| Dynamic gas reserve + drain to payout | `SWEEP` (all funds + refund redirect) |
| 24.8 KB inline `pk`+`sig`, ~5M gas verify | stripped signature + aggregated witness |
| Single-chain assumptions | per-chain: L1 programmable, some L2s keystore-only |
| Operator-gated sponsor (one operator, ECDSA gate) | native payment frames + `vault_sender`; permissionless paymaster market |
