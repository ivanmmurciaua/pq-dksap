// SPDX-License-Identifier: MIT
pragma solidity 0.8.23;

/// Persistent post-quantum vault.
///
/// Holds funds swept in from stealth accounts and releases them only on a valid
/// ML-DSA signature, verified onchain by the shared ZKNOX singleton. Unlike the
/// self-paying stealth account, verification runs here in NORMAL execution (not
/// the gas-capped validation prefix), so the signature is a HARD gate: a call
/// without a valid signature reverts (see SECURITY.md, Finding 1). This is the
/// sound, reusable PQ payout.
///
/// Anyone may submit `withdraw` and pay the gas (a relayer); the signature binds
/// the destination and amount, so the submitter cannot steal or redirect. A
/// per-vault nonce prevents replay.
interface ISingleton {
    function verifyInline(bytes calldata pk, bytes32 m, bytes calldata sig)
        external
        view
        returns (bytes4);
}

contract Vault {
    /// keccak256 of the owner's expanded ML-DSA public key. Binds this vault to
    /// exactly one post-quantum key; the inline pk at withdraw must match.
    bytes32 public immutable commit;
    address public immutable singleton;
    uint256 public nonce;

    /// Valid-result sentinel returned by ZKNOX verifyInline.
    bytes4 private constant VALID = 0x024ad318;

    event Withdrawn(address indexed dest, uint256 amount, uint256 indexed spentNonce);

    constructor(bytes32 _commit, address _singleton) {
        commit = _commit;
        singleton = _singleton;
    }

    /// Accept deposits (stealth sweeps and plain transfers) with no gating.
    receive() external payable {}

    /// Move `amount` to `dest`, authorized by an ML-DSA signature over a message
    /// that binds dest, amount, this vault, the chain, and the current nonce.
    /// `pk` is the inline expanded public key; it must hash to `commit`.
    function withdraw(
        address payable dest,
        uint256 amount,
        bytes calldata pk,
        bytes calldata sig
    ) external {
        require(keccak256(pk) == commit, "vault: wrong key");
        bytes32 m = keccak256(
            abi.encode(dest, amount, nonce, address(this), block.chainid)
        );
        require(
            ISingleton(singleton).verifyInline(pk, m, sig) == VALID,
            "vault: bad signature"
        );
        uint256 spent = nonce;
        unchecked {
            nonce = spent + 1;
        }
        (bool ok, ) = dest.call{value: amount}("");
        require(ok, "vault: transfer failed");
        emit Withdrawn(dest, amount, spent);
    }
}
