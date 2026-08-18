// Hermes Bot Mode exposes its operator UI through the native Desktop plugin.
// The legacy web dashboard still loads every enabled dashboard manifest,
// including hidden tabs, and expects each script to register successfully.
// Register a non-visible component so backend API discovery stays healthy
// without exposing a duplicate web-dashboard surface.
window.__HERMES_PLUGINS__?.register("hermes-bots", () => null)
