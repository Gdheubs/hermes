/**
 * The dashboard voice card must ask the gateway for a real sidecar session —
 * invented session ids 404 at prompt.submit (turn attribution itself is
 * covered by apps/shared voice-supervisor tests).
 */
import { describe, expect, it } from "vitest";

import { voiceCallSessionCreateParams } from "./VoiceCallCard";

describe("voiceCallSessionCreateParams", () => {
  it("requests a reaped sidecar session and forwards profile", () => {
    expect(voiceCallSessionCreateParams()).toEqual({
      close_on_disconnect: true,
      source: "tool",
    });
    expect(voiceCallSessionCreateParams("coder")).toEqual({
      close_on_disconnect: true,
      source: "tool",
      profile: "coder",
    });
  });
});
