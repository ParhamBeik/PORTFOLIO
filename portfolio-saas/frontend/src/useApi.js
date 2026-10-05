import { useCallback, useEffect, useRef, useState } from "react";
import { trackLoad } from "./perf.js";

// The one fetch pattern. Every old component hand-rolled `let current = true`,
// a `retryKey` counter and its own error string; this replaces all of it.
//
//   const { data, error, loading, reload } = useApi(
//     () => myOptimal(account), [account]
//   );
//
// `pollMs` refetches on an interval without flashing the loading state, for the
// dashboard's live valuation. Polling pauses while the tab is hidden.
//
// `timeoutMs` bounds how long a request may sit in `loading` with nothing to
// show: some backend views can take far longer than a user should ever stare
// at a spinner (or, worst case, drop the connection without a clean HTTP
// error). Past the deadline this surfaces a retryable error instead of
// hanging forever; if the original request eventually does resolve, its
// result still lands normally and replaces the timeout error.
const DEFAULT_TIMEOUT_MS = 25000;

export function useApi(
  fn,
  deps = [],
  { enabled = true, pollMs = 0, pauseWhenHidden = true, timeoutMs = DEFAULT_TIMEOUT_MS } = {}
) {
  const [state, setState] = useState({ data: null, error: null, loading: enabled });
  const [nonce, setNonce] = useState(0);
  const fnRef = useRef(fn);
  fnRef.current = fn;

  const reload = useCallback(() => setNonce((n) => n + 1), []);

  useEffect(() => {
    if (!enabled) {
      setState({ data: null, error: null, loading: false });
      return;
    }
    let live = true;
    setState((s) => ({ ...s, loading: true, error: null }));

    const run = (quiet = false) => {
      let settled = false;
      // Initial loads count toward the page-ready measurement; polls do not.
      const loaded = quiet ? () => {} : trackLoad();
      const timer = timeoutMs
        ? setTimeout(() => {
            if (!live || settled) return;
            setState((s) =>
              quiet && s.data
                ? s
                : {
                    data: null,
                    error: new Error("This is taking longer than expected."),
                    loading: false,
                  }
            );
          }, timeoutMs)
        : null;

      return fnRef.current()
        .then((data) => {
          settled = true;
          loaded();
          if (timer) clearTimeout(timer);
          if (live) setState({ data, error: null, loading: false });
        })
        .catch((error) => {
          settled = true;
          loaded();
          if (timer) clearTimeout(timer);
          if (!live) return;
          setState((s) =>
            quiet && s.data ? s : { data: null, error, loading: false }
          );
        });
    };

    run();
    if (!pollMs) return () => { live = false; };

    const id = setInterval(() => {
      if (pauseWhenHidden && document.hidden) return;
      run(true);
    }, pollMs);
    return () => {
      live = false;
      clearInterval(id);
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [...deps, enabled, pollMs, pauseWhenHidden, timeoutMs, nonce]);

  return { ...state, reload };
}
