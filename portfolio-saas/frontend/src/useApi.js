import { useCallback, useEffect, useRef, useState } from "react";

// The one fetch pattern. Every old component hand-rolled `let current = true`,
// a `retryKey` counter and its own error string; this replaces all of it.
//
//   const { data, error, loading, reload } = useApi(
//     () => myOptimal(account), [account]
//   );
//
// `pollMs` refetches on an interval without flashing the loading state, for the
// dashboard's live valuation. Polling pauses while the tab is hidden.
export function useApi(fn, deps = [], { enabled = true, pollMs = 0, pauseWhenHidden = true } = {}) {
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

    const run = (quiet = false) =>
      fnRef.current()
        .then((data) => live && setState({ data, error: null, loading: false }))
        .catch((error) => {
          if (!live) return;
          setState((s) =>
            quiet && s.data ? s : { data: null, error, loading: false }
          );
        });

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
  }, [...deps, enabled, pollMs, pauseWhenHidden, nonce]);

  return { ...state, reload };
}
