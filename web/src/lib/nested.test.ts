import { describe, expect, it } from "vitest";
import { getNestedValue, setNestedValue } from "./nested";

describe("nested configuration paths", () => {
  it.each([
    "terminal.backends.coder.__proto__.polluted",
    "terminal.backends.coder.constructor.prototype.polluted",
    "terminal.backends.coder..polluted",
  ])("rejects unsafe writes through %s", (path) => {
    expect(() => setNestedValue({}, path, true)).toThrow(
      "Unsafe nested configuration path",
    );
    expect((Object.prototype as Record<string, unknown>).polluted).toBeUndefined();
  });

  it("does not traverse inherited properties when reading", () => {
    const inherited = { inherited: "secret" };
    const value = Object.create(inherited) as Record<string, unknown>;

    expect(getNestedValue(value, "inherited")).toBeUndefined();
  });

  it("reads and writes ordinary nested values", () => {
    const updated = setNestedValue({}, "terminal.backends.coder.workspace", "dev");

    expect(getNestedValue(updated, "terminal.backends.coder.workspace")).toBe(
      "dev",
    );
  });
});
