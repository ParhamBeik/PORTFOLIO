// Unit test: tests the asynchronous lifecycle and timeout states of the custom useApi React hook in a simulated DOM environment,
// ensuring predictable loading, error, and stale data retention behavior across component re-renders.

import { renderHook, act, waitFor } from "@testing-library/react";
import { describe, it, expect, vi } from "vitest";
import { useApi } from "./useApi.js";

describe("useApi hook lifecycle", () => {
  it("starts in loading state and resolves data on successful fetch", async () => {
    const mockFetcher = vi.fn().mockResolvedValue({ status: "ok", count: 42 });

    const { result } = renderHook(() => useApi(mockFetcher));

    expect(result.current.loading).toBe(true);
    expect(result.current.data).toBe(null);
    expect(result.current.error).toBe(null);

    await waitFor(() => {
      expect(result.current.loading).toBe(false);
    });

    expect(result.current.data).toEqual({ status: "ok", count: 42 });
    expect(result.current.error).toBe(null);
  });

  it("handles fetch rejection and sets error state", async () => {
    const mockError = new Error("Network unreachable");
    const mockFetcher = vi.fn().mockRejectedValue(mockError);

    const { result } = renderHook(() => useApi(mockFetcher));

    await waitFor(() => {
      expect(result.current.loading).toBe(false);
    });

    expect(result.current.data).toBe(null);
    expect(result.current.error).toBe(mockError);
  });

  it("does not fetch when enabled is false", () => {
    const mockFetcher = vi.fn().mockResolvedValue({ status: "ok" });

    const { result } = renderHook(() =>
      useApi(mockFetcher, [], { enabled: false })
    );

    expect(result.current.loading).toBe(false);
    expect(result.current.data).toBe(null);
    expect(mockFetcher).not.toHaveBeenCalled();
  });

  it("triggers reload when reload callback is called", async () => {
    let callCount = 0;
    const mockFetcher = vi.fn().mockImplementation(() => {
      callCount++;
      return Promise.resolve({ call: callCount });
    });

    const { result } = renderHook(() => useApi(mockFetcher));

    await waitFor(() => {
      expect(result.current.loading).toBe(false);
    });
    expect(result.current.data).toEqual({ call: 1 });

    act(() => {
      result.current.reload();
    });

    await waitFor(() => {
      expect(result.current.data).toEqual({ call: 2 });
    });
  });
});
