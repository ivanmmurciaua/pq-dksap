// EIP-8141 post-quantum stealth ACCOUNT (ethrex Hegota dialect).
//
// The account is the tx sender. A spend is one frame transaction:
//   frame 0  VERIFY  : cheap self-approval (fits the 100k MAX_VERIFY_GAS cap)
//   frame 1  DEFAULT : the ~5M ML-DSA verify (too heavy for a VERIFY frame),
//                      reverts the whole tx unless the signature is valid
//   frame 2  SENDER  : moves the value (only reached if the DEFAULT gate passed)
//
// The public key travels inline in the ARBITRARY signature (no per-account
// PKContract / SSTORE2). The account's key commitment (keccak256 of the expanded
// public key) is supplied as a 32-byte constructor argument and kept in storage,
// so one compiled artifact serves every stealth key.
//
// NOTE: Yul `verbatim` places the FIRST argument on TOP of the stack.
object "Account" {
    code {
        // constructor arg: 32-byte key commitment appended to the init code
        codecopy(0, sub(codesize(), 32), 32)
        sstore(0, mload(0))
        let sz := datasize("runtime")
        datacopy(0, dataoffset("runtime"), sz)
        return(0, sz)
    }
    object "runtime" {
        code {
            // frame 0 (VERIFY): approve execution + payment, cheaply, then stop.
            if iszero(verbatim_1i_1o(hex"b0", 0x0a)) {   // TXPARAM(current_frame_index) == 0
                verbatim_3i_0o(hex"aa", 0, 0, 3)         // APPROVE(offset=0, length=0, scope=3)
                stop()
            }

            // frame 1 (DEFAULT): heavy ML-DSA verification, revert-gated.
            let SINGLETON := 0x06c03c98c9c6223787cc21ae0dd386312eb94813
            let PKLEN := 22400
            let SIGLEN := 2420

            let sigHash := verbatim_1i_1o(hex"b0", 0x08)   // TXPARAM(sig_hash)

            mstore(0, shl(224, 0xc34781db))          // verifyInline(bytes,bytes32,bytes) selector
            mstore(4, 0x60)                          // head: offset to pk
            mstore(36, sigHash)                      // head: m
            mstore(68, add(0x80, PKLEN))             // head: offset to sig
            mstore(100, PKLEN)                       // pk length
            verbatim_4i_0o(hex"b5", 132, 0, PKLEN, 0)          // SIGDATACOPY pk -> mem[132]
            let sigLenPos := add(132, PKLEN)
            mstore(sigLenPos, SIGLEN)                // sig length
            let sigPos := add(sigLenPos, 32)
            verbatim_4i_0o(hex"b5", sigPos, PKLEN, SIGLEN, 0)  // SIGDATACOPY sig -> mem[sigPos]
            let inLen := add(sigPos, 2432)           // SIGLEN padded to a 32-byte multiple

            // binding: the expanded public key must match this account's commitment
            if iszero(eq(keccak256(132, PKLEN), sload(0))) { revert(0, 0) }

            let ok := staticcall(gas(), SINGLETON, 0, inLen, inLen, 32)
            if iszero(ok) { revert(0, 0) }
            if iszero(eq(mload(inLen), shl(224, 0x024ad318))) { revert(0, 0) }
            stop()   // gate passed; the SENDER frame moves the value
        }
    }
}
