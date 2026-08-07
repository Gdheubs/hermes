// @vitest-environment jsdom
import { act, type ReactNode } from "react";
import { createRoot, type Root } from "react-dom/client";
import { MemoryRouter } from "react-router";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { PTY_TICKET_TIMEOUT_MS } from "@/lib/pty-reconnect";

class FakeFitAddon {
  static instances: FakeFitAddon[] = [];
  fit = vi.fn();

  constructor() {
    FakeFitAddon.instances.push(this);
  }
}

class FakeWebglAddon {
  onContextLoss() {
    return { dispose() {} };
  }
}

class FakeTerminal {
  static instances: FakeTerminal[] = [];
  options: Record<string, unknown>;
  rows = 24;
  cols = 80;
  parser = {
    registerOscHandler: vi.fn(),
  };
  unicode = { activeVersion: "" };
  refresh = vi.fn();

  constructor(options: Record<string, unknown>) {
    this.options = options;
    FakeTerminal.instances.push(this);
  }

  attachCustomKeyEventHandler() {
    return true;
  }

  attachCustomWheelEventHandler() {
    return true;
  }

  clearSelection() {}

  dispose() {}

  focus() {}

  getSelection() {
    return "";
  }

  loadAddon() {}

  onData() {
    return { dispose() {} };
  }

  onResize() {
    return { dispose() {} };
  }

  onScroll() {
    return { dispose() {} };
  }

  get buffer() {
    // Minimal active-buffer surface for the resume follow-scroll pin
    // (isViewportPinnedToBottom reads viewportY/baseY).
    return { active: { baseY: 0, viewportY: 0 } };
  }

  scrollToBottom() {}

  open(host: HTMLElement) {
    // Mimic xterm.js: a scrollable .xterm-viewport child is created so
    // the touch-scroll handler can locate it (#81119).
    const viewport = document.createElement("div");
    viewport.className = "xterm-viewport";
    host.appendChild(viewport);
  }

  paste() {}

  write() {}
}

const maybeReloadForLoopbackWsAuthFailure = vi.fn(() => false);
const apiMocks = vi.hoisted(() => ({
  buildWsUrl: vi.fn(async () => "ws://localhost/api/pty?channel=chat-1"),
}));

vi.mock("@xterm/addon-fit", () => ({ FitAddon: FakeFitAddon }));
vi.mock("@xterm/addon-unicode11", () => ({ Unicode11Addon: class {} }));
vi.mock("@xterm/addon-web-links", () => ({ WebLinksAddon: class {} }));
vi.mock("@xterm/addon-webgl", () => ({ WebglAddon: FakeWebglAddon }));
vi.mock("@xterm/xterm", () => ({ Terminal: FakeTerminal }));
vi.mock("@/components/ChatSidebar", () => ({
  ChatSidebar: () => null,
}));
vi.mock("@/components/ChatSessionList", () => ({
  ChatSessionList: () => null,
}));
vi.mock("@/components/Backdrop", () => ({ Backdrop: () => null }));
vi.mock("@/plugins", () => ({
  PluginSlot: () => null,
}));
vi.mock("@/contexts/usePageHeader", () => ({
  usePageHeader: () => ({ setEnd: vi.fn(), setTitle: vi.fn() }),
}));
vi.mock("@/contexts/useProfileScope", () => ({
  useProfileScope: () => ({ profile: "" }),
}));
vi.mock("@/themes", () => ({
  useTheme: () => ({ theme: { terminalBackground: "#000000" } }),
}));
vi.mock("@/i18n", () => ({
  useI18n: () => ({
    t: {
      app: {
        closeModelTools: "Close model tools",
        modelToolsSheetSubtitle: "Tools",
        modelToolsSheetTitle: "Model",
      },
    },
  }),
}));
vi.mock("@/lib/dashboard-auth-reload", () => ({
  maybeReloadForLoopbackWsAuthFailure,
}));
vi.mock("@/lib/api", () => ({
  api: apiMocks,
  buildWsUrl: apiMocks.buildWsUrl,
}));

class FakeWebSocket {
  static instances: FakeWebSocket[] = [];
  static OPEN = 1;

  binaryType = "blob";
  onclose: ((event: CloseEventLike) => void) | null = null;
  onmessage: ((event: { data: ArrayBuffer | string }) => void) | null = null;
  onopen: (() => void) | null = null;
  readyState = FakeWebSocket.OPEN;
  url: string;

  constructor(url: string) {
    this.url = url;
    FakeWebSocket.instances.push(this);
  }

  close() {
    this.readyState = 3;
  }

  send() {}
}

type CloseEventLike = {
  code: number;
  reason: string;
  wasClean: boolean;
};

let container: HTMLDivElement;
let root: Root;

// jsdom runs without an origin here (per-file @vitest-environment jsdom on a
// node-default config), so localStorage is undefined. Stub it so components
// that persist UI state (side panel collapse) can be exercised.
const localStorageMock = (() => {
  let store: Record<string, string> = {};
  return {
    getItem: (key: string) => store[key] ?? null,
    setItem: (key: string, value: string) => {
      store[key] = String(value);
    },
    removeItem: (key: string) => {
      delete store[key];
    },
    clear: () => {
      store = {};
    },
  };
})();

async function render(ui: ReactNode) {
  container = document.createElement("div");
  document.body.append(container);
  root = createRoot(container);
  await act(async () => root.render(ui));
}

beforeEach(() => {
  FakeWebSocket.instances = [];
  FakeFitAddon.instances = [];
  FakeTerminal.instances = [];
  maybeReloadForLoopbackWsAuthFailure.mockClear();
  apiMocks.buildWsUrl.mockReset();
  apiMocks.buildWsUrl.mockResolvedValue("ws://localhost/api/pty?channel=chat-1");
  vi.stubGlobal("WebSocket", FakeWebSocket);
  vi.stubGlobal(
    "ResizeObserver",
    class {
      disconnect() {}
      observe() {}
      unobserve() {}
    },
  );
  vi.stubGlobal("requestAnimationFrame", (cb: FrameRequestCallback) => {
    cb(0);
    return 1;
  });
  vi.stubGlobal("cancelAnimationFrame", () => {});
  vi.stubGlobal("matchMedia", () => ({
    addEventListener() {},
    matches: false,
    media: "",
    removeEventListener() {},
  }));
  vi.stubGlobal("crypto", {
    getRandomValues: (values: Uint8Array) => {
      values.fill(7);
      return values;
    },
    randomUUID: () => "chat-test-id",
  });

  Object.defineProperty(window, "visualViewport", {
    configurable: true,
    value: { addEventListener() {}, removeEventListener() {}, width: 1280 },
  });
  Object.defineProperty(window, "__HERMES_SESSION_TOKEN__", {
    configurable: true,
    value: "stale-token",
    writable: true,
  });
  Object.defineProperty(window, "__HERMES_AUTH_REQUIRED__", {
    configurable: true,
    value: false,
    writable: true,
  });
  Object.defineProperty(window.navigator, "clipboard", {
    configurable: true,
    value: {
      readText: vi.fn(async () => ""),
      writeText: vi.fn(async () => {}),
    },
  });
  sessionStorage.clear();
  vi.stubGlobal("localStorage", localStorageMock);
  localStorageMock.clear();
});

afterEach(async () => {
  await act(async () => root?.unmount());
  container?.remove();
  vi.unstubAllGlobals();
});

describe("ChatPage", () => {
  it("treats loopback 4401 closes as stale-token reload candidates", async () => {
    const { default: ChatPage } = await import("./ChatPage");

    await render(
      <MemoryRouter initialEntries={["/chat"]}>
        <ChatPage isActive />
      </MemoryRouter>,
    );

    await vi.waitFor(() => expect(FakeWebSocket.instances).toHaveLength(1));

    FakeWebSocket.instances[0].onclose?.({
      code: 4401,
      reason: "auth: token_mismatch",
      wasClean: true,
    });

    expect(maybeReloadForLoopbackWsAuthFailure).toHaveBeenCalledWith(4401);
  });

  it("lets touch swipes scroll the xterm scrollback natively (#81119)", async () => {
    const { default: ChatPage } = await import("./ChatPage");

    await render(
      <MemoryRouter initialEntries={["/chat"]}>
        <ChatPage isActive />
      </MemoryRouter>,
    );

    const host = document.querySelector(
      ".hermes-chat-xterm-host",
    ) as HTMLElement | null;
    expect(host).not.toBeNull();
    const viewport = host!.querySelector<HTMLElement>(".xterm-viewport");
    expect(viewport).not.toBeNull();
    // The terminal pane explicitly opts in to native vertical panning so the
    // browser owns the scroll gesture.
    expect(viewport!.style.touchAction).toBe("pan-y");

    // Simulate xterm.js's own target-phase handler, which would call
    // preventDefault() on touchmove and swallow the swipe. The capture-phase
    // listener must stop propagation before that handler fires.
    const xtermTouchMove = vi.fn((ev: Event) => ev.preventDefault());
    viewport!.addEventListener("touchmove", xtermTouchMove);

    const child = document.createElement("div");
    viewport!.appendChild(child);

    const dispatch = () => {
      const ev = new Event("touchmove", { bubbles: true, cancelable: true });
      Object.defineProperty(ev, "touches", {
        configurable: true,
        value: [{ identifier: 1 }],
      });
      child.dispatchEvent(ev);
      return ev;
    };
    const ev = dispatch();

    expect(xtermTouchMove).not.toHaveBeenCalled();
    expect(ev.defaultPrevented).toBe(false);

    // A direct touchmove on the viewport itself (no child) must also be
    // intercepted — xterm's preventDefault is bound on the viewport.
    viewport!.removeEventListener("touchmove", xtermTouchMove);
    viewport!.addEventListener("touchmove", xtermTouchMove);
    const ev2 = new Event("touchmove", { bubbles: true, cancelable: true });
    Object.defineProperty(ev2, "touches", {
      configurable: true,
      value: [{ identifier: 1 }],
    });
    viewport!.dispatchEvent(ev2);
    expect(xtermTouchMove).not.toHaveBeenCalled();
    expect(ev2.defaultPrevented).toBe(false);
  });

  it("refits and repaints the terminal after a viewport resize (#81119)", async () => {
    const { default: ChatPage } = await import("./ChatPage");

    await render(
      <MemoryRouter initialEntries={["/chat"]}>
        <ChatPage isActive />
      </MemoryRouter>,
    );

    const host = document.querySelector(
      ".hermes-chat-xterm-host",
    ) as HTMLElement | null;
    expect(host).not.toBeNull();

    // jsdom doesn't lay out elements, so clientWidth/Height are 0 by
    // default — the metrics sync early-returns while hidden.  Mock the
    // layout to simulate a mobile viewport and trigger a real refit.
    Object.defineProperty(host!, "clientWidth", {
      configurable: true,
      value: 800,
    });
    Object.defineProperty(host!, "clientHeight", {
      configurable: true,
      value: 600,
    });

    const terminal = FakeTerminal.instances[FakeTerminal.instances.length - 1];
    const fitAddon = FakeFitAddon.instances[FakeFitAddon.instances.length - 1];
    fitAddon.fit.mockClear();
    terminal.refresh.mockClear();

    act(() => {
      window.dispatchEvent(new Event("resize"));
    });

    // scheduleSyncTerminalMetrics debounces 60ms before calling fit.
    await vi.waitFor(() => expect(fitAddon.fit).toHaveBeenCalled());

    // The width changed enough to cross a font tier, so the explicit
    // refresh must fire as well — otherwise the canvas can stay stale on
    // touch devices until something else forces a repaint.
    expect(terminal.refresh).toHaveBeenCalledWith(0, terminal.rows - 1);
  });
});

describe("ChatPage side panel collapse", () => {
  async function renderChat() {
    const { default: ChatPage } = await import("./ChatPage");
    await render(
      <MemoryRouter initialEntries={["/chat"]}>
        <ChatPage isActive />
      </MemoryRouter>,
    );
  }

  it("collapses the desktop side panel and persists the choice", async () => {
    localStorage.clear();
    await renderChat();
    await vi.waitFor(() => expect(FakeWebSocket.instances).toHaveLength(1));

    const collapseButton = container.querySelector(
      '[aria-label="Collapse chat side panel"]',
    );
    expect(collapseButton).not.toBeNull();

    await act(async () => {
      collapseButton!.dispatchEvent(
        new MouseEvent("click", { bubbles: true }),
      );
    });

    expect(localStorage.getItem("hermes-chat-panel-collapsed")).toBe("1");
    expect(
      container.querySelector('[aria-label="Collapse chat side panel"]'),
    ).toBeNull();
    expect(
      container.querySelector('[aria-label="Show chat side panel"]'),
    ).not.toBeNull();

    // Reopening restores the panel and clears the persisted flag.
    await act(async () => {
      container
        .querySelector('[aria-label="Show chat side panel"]')!
        .dispatchEvent(new MouseEvent("click", { bubbles: true }));
    });

    expect(localStorage.getItem("hermes-chat-panel-collapsed")).toBe("0");
    expect(
      container.querySelector('[aria-label="Collapse chat side panel"]'),
    ).not.toBeNull();
  });
});

// The gated-mode ticket request runs before any socket exists, so a rejection
// or a hang emits no `close` event and never arms PTY_CONNECTING_TIMEOUT_MS
// (that timer is set after `new WebSocket`). Without its own deadline the tab
// strands on "connecting" with no retry. Mirrors the ChatSidebar events-feed
// coverage in src/components/ChatSidebar.test.tsx.
describe("ChatPage PTY ticket connect deadline", () => {
  beforeEach(() => {
    vi.useFakeTimers();
  });

  afterEach(() => {
    vi.useRealTimers();
  });

  async function renderChat() {
    const { default: ChatPage } = await import("./ChatPage");
    await render(
      <MemoryRouter initialEntries={["/chat"]}>
        <ChatPage isActive />
      </MemoryRouter>,
    );
  }

  /** Advance timers and flush the async connect that fires on the tick. */
  async function advance(ms: number) {
    await act(async () => {
      await vi.advanceTimersByTimeAsync(ms);
    });
  }

  it("retries when the ticket request rejects", async () => {
    apiMocks.buildWsUrl.mockRejectedValueOnce(
      new Error("ticket endpoint unavailable"),
    );

    await renderChat();
    await advance(0);
    expect(FakeWebSocket.instances).toHaveLength(0);

    // First backoff step is 250ms; the retry must mint a fresh ticket.
    await advance(250);
    expect(apiMocks.buildWsUrl).toHaveBeenCalledTimes(2);
    expect(FakeWebSocket.instances).toHaveLength(1);
  });

  it("times out a stalled ticket request and retries", async () => {
    let resolveStalledRequest!: (url: string) => void;
    apiMocks.buildWsUrl.mockImplementationOnce(
      () =>
        new Promise<string>((resolve) => {
          resolveStalledRequest = resolve;
        }),
    );

    await renderChat();
    await advance(0);
    expect(FakeWebSocket.instances).toHaveLength(0);

    await advance(PTY_TICKET_TIMEOUT_MS);
    expect(FakeWebSocket.instances).toHaveLength(0);

    // A late ticket from the timed-out attempt must not open a socket behind
    // the replacement the deadline scheduled.
    await act(async () => {
      resolveStalledRequest("ws://localhost/api/pty?channel=stale");
      await Promise.resolve();
    });
    expect(FakeWebSocket.instances).toHaveLength(0);

    await advance(250);
    expect(FakeWebSocket.instances).toHaveLength(1);
    expect(FakeWebSocket.instances[0].url).not.toContain("channel=stale");
  });

  it("leaves a settled ticket's socket to the CONNECTING timer", async () => {
    await renderChat();
    await advance(0);
    await vi.waitFor(() => expect(FakeWebSocket.instances).toHaveLength(1));

    // NS-591 regression: once the socket exists the ticket deadline is
    // disarmed, so PTY_CONNECTING_TIMEOUT_MS stays the only thing that may
    // force-close a wedged handshake — the two must not both fire.
    await advance(PTY_TICKET_TIMEOUT_MS);
    expect(apiMocks.buildWsUrl).toHaveBeenCalledTimes(1);
  });
});
