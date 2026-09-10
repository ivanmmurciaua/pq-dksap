// Shared client state. A single mutable object imported by every module (ES
// module imports are live bindings, so all modules see the same instance).
export const state = { handle: null, address: null };
