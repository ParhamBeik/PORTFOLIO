# Agent notes

Before changing anything that stores data (tables, hypertables, buckets, volumes, caches), read and follow [portfolio-saas/docs/STORAGE-POLICY.md](portfolio-saas/docs/STORAGE-POLICY.md). The VPS has no backups and a 100 GB disk shared by four apps. Portfolio history is kept in full and compressed losslessly; never thin it or replace it with links.

## Before/after proof in PRs

Every PR that changes what users see, or what the API returns, carries proof in its
description, so a reviewer (a person, the Codex review or a cloud agent) can judge it
without running it:

- Screens: a Before / After table of screenshots. "Before" is the live site
  (https://portfolio.parhambm.ir) or `main`; "after" is the fixed branch running locally. Use the same page,
  data and window size on both sides, and add phone width (393 px) when the layout changes.
- Backend: the same request before and after, with one line on why the "after" value is right.
- Upload the images through GitHub's web page; never commit screenshots to the repo.
  A change whose proof is not ready yet says "not shown" and why.
