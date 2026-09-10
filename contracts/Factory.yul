// Minimal CREATE2 deployer for counterfactual stealth accounts.
//
// A stealth account's address must be predictable BEFORE it is deployed, so a
// payer can send funds to it without any interaction. CREATE2 gives that: the
// address is keccak256(0xff ++ factory ++ salt ++ keccak256(init_code))[12:],
// a pure function of public inputs. The payer funds the bare address; the
// recipient deploys the account here later, at that exact address, then sweeps.
//
// Calldata layout: salt (32 bytes) || init_code (rest).
// Returns the 20-byte deployed address; reverts if CREATE2 fails.
// Any msg.value is forwarded to the new account (unused in the split flow,
// where the payer funds the address directly).
object "Factory" {
    code {
        datacopy(0, dataoffset("runtime"), datasize("runtime"))
        return(0, datasize("runtime"))
    }
    object "runtime" {
        code {
            let salt := calldataload(0)
            let size := sub(calldatasize(), 32)
            calldatacopy(0, 32, size)
            let addr := create2(callvalue(), 0, size, salt)
            if iszero(addr) { revert(0, 0) }
            mstore(0, addr)
            return(12, 20)
        }
    }
}
