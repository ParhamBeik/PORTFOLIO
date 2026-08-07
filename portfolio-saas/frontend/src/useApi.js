import { useCallback, useEffect, useRef, useState } from "react";

// The one fetch pattern. Every old component hand-rolled `let current = true`,
// a `retryKey` counter and its own error string; this replaces all of it.
//
//   const { data, error, loading, reload, proRequired } = useApi(
//     () => myOptimal(account), [account]
//   );
//
// `proRequired` is a 403 from the backend's RequiresFeature gate. It is the ONLY
// tier signal in the frontend — there is deliberately no `user.is_pro` check
// anywhere, so the backend stays the single source of truth on entitlements.
//
// `pollMs` refetches on an interval without flashing the loading state, for the
// dashboard's live valuation.
export function useApi(fn, deps = [], { enabled = true, pollMs = 0 } = {}) {
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
          // A failed poll must not blank a screen that is already showing good
          // data — surface it only if we have nothing better.
          if (!live) return;
          setState((s) =>
            quiet && s.data ? s : { data: null, error, loading: false }
          );
        });

    run();
    if (!pollMs) return () => { live = false; };

    const id = setInterval(() => run(true), pollMs);
    return () => {
      live = false;
      clearInterval(id);
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [...deps, enabled, pollMs, nonce]);

  return { ...state, reload, proRequired: state.error?.status === 403 };
}
