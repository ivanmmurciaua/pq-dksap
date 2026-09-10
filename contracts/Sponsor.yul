// EIP-8141 gasless SPONSOR (ethrex Hegota dialect).
//
// The sponsor is the frame-tx SENDER. It runs in one VERIFY frame and approves
// execution + payment (scope 3) ONLY IF the operator authorized this exact
// transaction: an ECDSA signature over the canonical sig_hash, carried inline in
// an ARBITRARY signature entry (index 0), must ecrecover to the operator address.
// The operator address is a 32-byte constructor argument kept in storage, so one
// compiled artifact serves any operator. No / wrong signature -> revert, so
// nobody can burn the sponsor's balance on unauthorized transactions.
//
// When it approves, the sponsor becomes the gas payer for the whole tx (payer =
// frame.target = this account, and ethrex sets no payer==sender constraint), so
// the DEFAULT frames that follow (factory deploy, vault.withdraw) run with the
// end user paying zero ETH. The operator signature binds the sig_hash, so it
// authorizes exactly this tx's frames and cannot be replayed onto another.
//
// NOTE: Yul `verbatim` places the FIRST argument on TOP of the stack.
object "Sponsor" {
    code {
        // constructor arg: 32-byte operator address (low 160 bits) appended to init
        codecopy(0, sub(codesize(), 32), 32)
        sstore(0, mload(0))
        let sz := datasize("runtime")
        datacopy(0, dataoffset("runtime"), sz)
        return(0, sz)
    }
    object "runtime" {
        code {
            let sigHash := verbatim_1i_1o(hex"b0", 0x08)   // TXPARAM(sig_hash)

            // Operator signature: 65 bytes r||s||v (v = 27/28), inline in the
            // ARBITRARY signature entry at index 0.
            verbatim_4i_0o(hex"b5", 128, 0, 65, 0)         // SIGDATACOPY -> mem[128..193]
            let r := mload(128)
            let s := mload(160)
            let v := byte(0, mload(192))                   // the 65th byte

            // ecrecover(0x01) input: [hash, v, r, s]; recovered address -> mem[200]
            mstore(0, sigHash)
            mstore(32, v)
            mstore(64, r)
            mstore(96, s)
            let ok := staticcall(gas(), 0x01, 0, 128, 200, 32)
            if iszero(ok) { revert(0, 0) }
            if iszero(eq(mload(200), sload(0))) { revert(0, 0) }   // must be the operator

            verbatim_3i_0o(hex"aa", 0, 0, 3)               // APPROVE(offset=0, length=0, scope=3)
            stop()
        }
    }
}
