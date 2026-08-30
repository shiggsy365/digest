# E-reader client rewrite

## Why

The current Kobo/Kindle experience is a set of server-rendered Jinja templates
(`digest/templates/ereader/*.html`) that hand-roll a bookstack-style layout: a
fixed-height CSS table shell, a single-row `<table>` nav, and a JS helper
(`ereader.js`) that measures the viewport and paginates already-rendered rows
client-side. It works, but every layout fix so far has meant re-deriving a
technique bookstack already solved, from scratch, against digest's own markup.

Bookstack itself isn't a template we can drop in: it's a genuine single-page
app. Its shell (`index.html`) ships almost no server-rendered content — every
screen is fetched as JSON from bookstack's own API
(`/api/opds/...`, `/api/discovery/...`, `/api/shelfmark/queue`, ...) and
rendered into the DOM by its own JS (`renderPaginatedList`, `createBookItemDiv`,
`ajax()`, etc.). None of that can call digest's backend as-is, because digest
has a different schema and returns full HTML pages, not JSON.

This document scopes turning digest's e-reader mode into the same kind of
client-rendered app: a small static shell + a JSON API + a bookstack-derived
JS client, instead of a page-per-route Jinja stack. The **modern** browser
experience (`digest/templates/modern/*.html`, `app.css`, `app.js`) is not part
of this work and does not change — `render()` already dispatches to
`modern/*` or `ereader/*` per request based on `is_ereader_request()`, so the
modern path stays exactly as it is until the day the new ereader shell fully
replaces the old templates.

## Current state (for reference)

- One set of FastAPI route functions in `main.py` serves both experiences;
  `render()` picks the Jinja template family by user-agent.
- Ereader-only branches already live inside those shared routes (e.g. the
  `view` default override in `library()`, the larger ereader page sizes) —
  these exist only because ereader still renders through the same routes as
  modern. They go away once ereader stops using them.
- `ereader.css` / `ereader.js` implement: a fixed-row CSS table shell
  (`#app-shell`), a burger menu, an inline search toggle, a pinned filters
  row, and `.js-paginated` containers that page pre-fetched batches to fit
  the viewport (`fitDigestShell()`, `fitPagedLists()`).

## Target architecture

- **One shell page** (e.g. `ereader/app.html`) replaces every current
  `ereader/*.html` template. It ships the CSS/JS once and never reloads for
  navigation between sections — only for login/setup and for things that are
  fine as a real page load (e.g. file downloads).
- **A JSON API**, additive to the existing HTML routes, under something like
  `/api/ereader/...`. Reuses the same session cookie and CSRF helper the HTML
  routes already use — no new auth mechanism. Each endpoint wraps existing
  business logic (`shelf_books()`, `build_discovery()`, `hardcover_books()`,
  `nyt_*`, `search_discovery_books()`, `acquisition.py`, `kobo.py`,
  `accounts.py`, `admin_settings.py`) and returns JSON instead of rendering a
  template.
- **A client script** (`ereader-app.js`) ported from bookstack's proven
  patterns — `fitViewport()`/`calculatePageSize()`-style dynamic pagination,
  `ajax()` helper, a simple view router (`switchTab`/`showTabView`) — adapted
  to digest's actual data shapes (books, shelves, kobo sync settings, wanted
  items) rather than bookstack's (store, shelfmark queue, Kindle email). This
  isn't a copy-paste of bookstack's script; the *techniques* are reused, the
  *code* is digest-specific because the domains differ.

## Milestones

### M0 — Design decisions
- Freeze the JSON API contract: endpoint list, request/response shapes,
  pagination cursor convention.
- Decide the rollout mechanism: build behind a flag (e.g.
  `DIGEST_EREADER_SPA=1`) so the old templates stay the fallback until the
  new shell is confirmed on real devices, rather than a single irreversible
  cutover.
- Confirm which flows stay outside the SPA (login, setup, password reset are
  low-value to convert and fine as plain page loads).

### M1 — JSON API: Library & book detail
- `/api/ereader/library` (filters: view, author, series, q, sort, direction,
  metadata + pagination), `/api/ereader/library/authors`,
  `/api/ereader/library/series`, `/api/ereader/books/{id}` (detail +
  prev/next in the current list context).
- Unit tests per endpoint, mirroring the existing route test style in
  `tests/test_library_views.py`.

### M2 — JSON API: Discover
- `/api/ereader/discover/for-you`, `/trending`, `/new-releases`,
  `/bestsellers` (+ `/bestsellers/lists`, `/bestsellers/weeks`), `/genre`,
  `/search`.
- Tests, including the no-API-key fallback paths already covered today.

### M3 — JSON API: Shelves, Downloads, Settings
- Shelves: list, detail, add/remove book.
- Downloads (wanted): list, queue a release, retry, cancel, remove.
- Settings: Kobo token issue/revoke, Kindle email, Kobo sync shelf/all-books
  toggle.
- Tests.

### M4 — Shell + client framework (walking skeleton)
- Build the static shell (`ereader/app.html`, `ereader-app.css`,
  `ereader-app.js`) with the same fixed-row table layout already proven in
  the current CSS, plus the nav/burger menu/search toggle already validated.
- Wire the `ajax()` helper and view router, and get exactly one section
  (Library) working end-to-end against the M1 API. This is the riskiest
  integration point (session auth over fetch/XHR, CSRF on JSON POSTs,
  dynamic pagination against real data) — validate it before building every
  other section against the same pattern.

### M5 — Port remaining sections
- Discover, Shelves, Downloads, Settings, Book detail, Search — each as a JS
  view module following the pattern from M4.
- Check each section against the artifact-preview workflow (or a
  staging/local instance) before wiring it into the flag-gated production
  path.

### M6 — Device validation
- Real Kobo/Kindle pass: nav, search, pagination, cover loading, Kobo device
  token flows, book download, reading-progress sync. Old WebKit engines are
  the entire reason this rewrite exists, so this milestone gates the cutover,
  not the other way around.

### M7 — Cutover & cleanup
- Flip the flag so ereader requests get the new shell by default.
- Remove the old `ereader/*.html` templates, `ereader.css`, `ereader.js`, and
  the ereader-only branches inside the shared HTML routes in `main.py` (the
  `view` default override, the bumped page sizes, etc.) now that ereader no
  longer renders through those routes at all.
- Full regression pass on **modern** to confirm it is unaffected — it never
  touches the new API or shell, but the shared route functions are getting
  edited to drop the ereader branches, so this is the one place regressions
  could leak in.

## Implemented contract

The rollout flag is `DIGEST_EREADER_SPA` and defaults to disabled. When enabled,
authenticated Kobo/Kindle requests receive `ereader/app.html`; login and setup
remain conventional pages. The modern template family is unchanged.

List endpoints use `page` (one-based) and `page_size` (maximum 100) and return
`items`, `page`, `page_size`, `total`, and `has_more` where the collection is
paginated. Mutations use the existing session cookie and require the shell's
CSRF value in the `X-CSRF-Token` header. The API is rooted at `/api/ereader` and
includes library/directories/detail, discovery, shelves, downloads, and profile
and Kobo settings endpoints.

Device validation and removal of the fallback templates remain rollout steps:
enable the flag on staging, validate on real devices, then make the flag the
deployment default before deleting the old implementation.

## Risks / open questions

- **Two client renderers to maintain.** Modern already has its own
  progressive-enhancement JS (`app.js`); this adds a second, unrelated client
  script for ereader. Worth deciding whether that's acceptable long-term or
  whether modern should eventually move to the same API (out of scope here).
- **Old JS engines.** Kobo/Kindle WebKit builds are old enough that `fetch`
  support, JSON parsing performance on large lists, and XHR quirks need
  checking early (M4), not assumed.
- **CSRF over JSON.** The existing CSRF helper is designed around
  form-encoded POSTs; state-changing JSON endpoints need the same protection
  without breaking on old browsers' `fetch`/XHR behavior.
- **Session-only auth is fine** — no token/JWT layer needed since the API and
  the shell are served to the same authenticated browser session.
