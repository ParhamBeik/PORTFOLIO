import { cleanup, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const getStatus = vi.fn(async () => ({ connected: true }));
const addNetworkListener = vi.fn(async () => ({ remove: vi.fn() }));
const addAppListener = vi.fn(async () => ({ remove: vi.fn() }));
const openExternal = vi.fn();

vi.mock("./mobile.js", () => ({
  API_ORIGIN: "https://portfolio.parhambm.ir",
  isNative: true,
  openExternal: (...args) => openExternal(...args),
}));

vi.mock("@capacitor/network", () => ({
  Network: {
    getStatus: (...args) => getStatus(...args),
    addListener: (...args) => addNetworkListener(...args),
  },
}));

vi.mock("@capacitor/app", () => ({
  App: {
    addListener: (...args) => addAppListener(...args),
    minimizeApp: vi.fn(),
  },
}));

const { default: MobileRuntime } = await import("./MobileRuntime.jsx");

afterEach(() => {
  cleanup();
  document.documentElement.classList.remove("native-app");
  getStatus.mockReset();
  addNetworkListener.mockReset();
  addAppListener.mockReset();
  openExternal.mockReset();
});

describe("MobileRuntime", () => {
  beforeEach(() => {
    getStatus.mockResolvedValue({ connected: true });
    addNetworkListener.mockResolvedValue({ remove: vi.fn() });
    addAppListener.mockResolvedValue({ remove: vi.fn() });
  });

  it("marks the document as a native shell and stays quiet while online", async () => {
    const { container } = render(<MobileRuntime />);
    await waitFor(() => expect(document.documentElement.classList.contains("native-app")).toBe(true));
    expect(container).toBeEmptyDOMElement();
  });

  it("shows the offline status banner when the device disconnects", async () => {
    getStatus.mockResolvedValue({ connected: false });
    render(<MobileRuntime />);
    expect(await screen.findByTestId("native-offline-status")).toHaveTextContent(/No connection/);
  });

  it("opens admin links through the system browser", async () => {
    render(<MobileRuntime />);
    await waitFor(() => expect(addAppListener).toHaveBeenCalled());
    const anchor = document.createElement("a");
    anchor.setAttribute("href", "/admin/");
    document.body.appendChild(anchor);
    anchor.dispatchEvent(new MouseEvent("click", { bubbles: true, cancelable: true }));
    await waitFor(() => expect(openExternal).toHaveBeenCalled());
    expect(String(openExternal.mock.calls[0][0])).toContain("/admin/");
    anchor.remove();
  });
});
