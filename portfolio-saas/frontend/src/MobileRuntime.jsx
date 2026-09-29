import { useEffect, useState } from "react";
import { App as NativeApp } from "@capacitor/app";
import { Network } from "@capacitor/network";
import { API_ORIGIN, isNative, openExternal } from "./mobile.js";

export default function MobileRuntime() {
  const [connected, setConnected] = useState(true);

  useEffect(() => {
    if (!isNative) return undefined;
    document.documentElement.classList.add("native-app");
    let live = true;
    let networkListener;
    let backListener;
    const updateConnection = (status) => {
      if (!live) return;
      setConnected(status.connected);
      window.dispatchEvent(new CustomEvent("holdings:network-change", { detail: status.connected }));
    };
    Network.getStatus().then(updateConnection);
    Network.addListener("networkStatusChange", (status) => {
      updateConnection(status);
    }).then((handle) => { if (live) networkListener = handle; else handle.remove(); });
    NativeApp.addListener("backButton", () => {
      if (document.querySelector('[role="dialog"]')) {
        document.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape", bubbles: true }));
      } else if (window.location.pathname !== "/") {
        if (window.history.length > 1) window.history.back();
        else window.location.assign("/");
      } else {
        NativeApp.minimizeApp();
      }
    }).then((handle) => { if (live) backListener = handle; else handle.remove(); });
    const onLink = (event) => {
      const anchor = event.target.closest?.("a[href]");
      if (!anchor || anchor.download || event.defaultPrevented) return;
      const url = new URL(anchor.href, window.location.href);
      if (url.protocol !== "https:" && url.protocol !== "http:") return;
      if (url.origin === window.location.origin && !url.pathname.startsWith("/admin/")) return;
      event.preventDefault();
      void openExternal(url.pathname.startsWith("/admin/") && url.origin === window.location.origin
        ? new URL(url.pathname + url.search + url.hash, import.meta.env.VITE_API_URL || API_ORIGIN).href
        : url.href);
    };
    document.addEventListener("click", onLink, true);
    return () => {
      live = false;
      document.removeEventListener("click", onLink, true);
      networkListener?.remove();
      backListener?.remove();
      document.documentElement.classList.remove("native-app");
    };
  }, []);

  if (!isNative || connected) return null;
  return <div className="native-connection" role="status" data-testid="native-offline-status">No connection · showing saved data where available</div>;
}
