import React from "react";
import { createRoot } from "react-dom/client";
import App from "./App.jsx";
import MobileRuntime from "./MobileRuntime.jsx";
import "./index.css";
import * as Sentry from "@sentry/react";

if (import.meta.env.VITE_SENTRY_DSN) {
  Sentry.init({
    dsn: import.meta.env.VITE_SENTRY_DSN,
    environment: import.meta.env.MODE,
    sendDefaultPii: false,
    tracesSampleRate: 0,
  });
}

createRoot(document.getElementById("root")).render(
  <React.StrictMode>
    <App />
    <MobileRuntime />
  </React.StrictMode>
);
