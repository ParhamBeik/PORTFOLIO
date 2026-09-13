# Reviewed change inventory

## Attributed and released

| Revision | State | Reviewed scope |
| --- | --- | --- |
| `55c41a9` | Merged into `main`, pushed, deployed as ancestor of `26ee30a` | Registration gate/status, public routes, auth form, legal views, comparison wording, dashboard risk loading, tests, production environment example. |
| `b439a4a` | Merged, pushed, deployed | Route-title test synchronization. |
| `6a419a7` | Merged, pushed, deployed | Lighthouse registration-status API stub. |
| `26ee30a` | Merged, pushed, deployed | Shared portfolio reload revision and insight refresh dependency. |

## Formerly pre-existing work, now reviewed and released

The multi-user/auth/operator/mail work was already dirty at review start and is not credited to
the recovered task. It was preserved, reviewed, completed with the API-backed sell-all
confirmation, committed as `c87eee3`, pushed to `main`, and deployed by `34740102439`.

## Review conclusion

No unrelated committed changes were found in the recovered task. The final release contains the
reviewed work and its evidence artifacts; the pre-release SHA is preserved on
`codex/preserve-multi-user-auth-mvp-20260913`.
