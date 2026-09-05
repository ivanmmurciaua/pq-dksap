"""Network and deployment configuration for the ethrex Hegota privacy testnet."""
import os

RPC_URL = os.environ.get("PQ_RPC_URL", "https://rpc1.privacy.ethrex.xyz")
FAUCET_URL = "https://faucet.privacy.ethrex.xyz/api/claim"
CHAIN_ID = 8141

# Shared, already-deployed post-quantum verifier (ZKNOX ETHDilithium + verifyInline).
# One instance serves every stealth account; the public key is passed inline.
SINGLETON = "0x06c03c98c9c6223787cc21ae0dd386312eb94813"

# ML-DSA-44 (Dilithium2) sizes for the ETH-optimized (keccak-PRNG) variant.
PK_DEPLOY_LEN = 22400   # expanded public key (A_hat || tr || t1)
SIG_LEN = 2420          # raw signature (cTilde || z || h)

GAS_PRICE = 10 ** 8   # 0.1 gwei (baseFee on this chain is a few wei)

# Repo-relative path to the compiled stealth account creation bytecode.
_HERE = os.path.dirname(__file__)
ACCOUNT_BIN = os.path.join(_HERE, "..", "contracts", "Account.bin")


def account_creation_code() -> bytes:
    with open(ACCOUNT_BIN) as f:
        return bytes.fromhex(f.read().strip())
