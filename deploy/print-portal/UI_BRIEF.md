# MAXCOURSE Print interface brief

Create only `print/index.html`, `print/print.css`, `print/print.js` and `deploy/print-portal/DESIGN.md`. Another engineer owns all Python, server, tests and deployment files. Do not edit them. Do not run git commits/pushes, install packages, access credentials, or read files outside this worktree. You may inspect `vendor/fonts.css` and existing public pages for brand reference. No real accounts or document contents are needed.

User requests a simple, beautifully designed Chinese campus printing portal. Use Kimi K3 for original visual/interaction work. This is a real product, not a static mockup. Build standalone semantic HTML, authored CSS, vanilla JS. No bundler, no remote CDNs, no external images/fonts. Local `/vendor/fonts.css` is available, as are system Chinese fonts. Prefer original restrained editorial typography, warm paper, graphite text, a very small MAXCOURSE lime accent, generous whitespace, fine separators and a quiet document motif. Avoid a stack of oversized rounded dashboard cards, decorative metrics, fake social proof and emoji icons. Mobile at 375px must be excellent. One clear primary action. Inline SVG icons permitted. Respect reduced motion and keyboard/focus/labels. No em/en dashes in Chinese prose.

Layout: slim brand header with back link to `/`, login identity/action; concise expressive heading and 1-line instruction; main document upload and preparation space; quiet paper/settings summary; school password and single submission CTA only when file is inspected; compact recent-task area and practical FAQ. Links to original `/print-setup/index.html` for driver/manual printing. The supported output is fixed A4, grayscale, one-sided, one copy. Accept PDF only, max 10 MiB and 50 pages. Do not expose nonfunctional options. Let the design show taste through typography, alignment and spacing, not ornamental clutter.

Status language: `submitted` means 已交给学校队列 / 请到打印机刷卡取件, never 打印完成. `unknown` means 提交结果待确认, instruct check school's release queue, no auto retry. Offline message does not imply upload will be queued. Password used only for this submission, not saved. Service has no printer release confirmation API. Do not claim zero queue time or prices. Explain school's charge is checked at release, not paid on this website.

## API contract

All requests same-origin, `credentials: 'same-origin'`. No tokens, passwords or PDF bodies in localStorage, sessionStorage, URLs, console, analytics or DOM attributes. Password input uses `autocomplete="off"`; clear immediately once dispatched. Keep file only in page memory, revoke object URLs if used. Never insert API text via innerHTML.

`GET /api/print/session` returns:
```
{
  "csrf_token":"opaque",
  "user":null | {"id":1,"display_name":"同学","school_username":"t12345678"},
  "limits":{"max_bytes":10485760,"max_pages":50},
  "capabilities":{"paper":"A4","color":"grayscale","sides":"one-sided","copies":1},
  "service":{"enabled":true,"online":true,"ready":true,"busy":false,"demo":false,"message":"可以提交打印"}
}
```
`ready=false` disables document inspection and submission and shows explanatory, calm status. `demo=true` must show 本地演示，不会真实打印. Recheck status periodically without overwriting selected file or password. Null user requires login. Empty school_username requires verified school login.

Login dialog may call existing `POST /api/login/ispace` JSON `{username,password}` and then refresh `/api/print/session`. Show server error text. This is separate from per-job school password. Do not automatically reuse login password to submit. Main page password form school username is read-only from session.

For mutation requests send JSON, `X-Print-CSRF: <csrf_token>` and `X-Print-User: <current user id>`. The latter prevents a stale tab submitting under another account after cookie identity changes.

`POST /api/print/inspect` body `{pdf: <raw-base64>}` returns `{pages:2,bytes:1234,sha256:"...",inspection_token:"..."}`. Parse actual PDF server-side, not by extension only. Frontend checks PDF extension, bytes size, no 0-byte file. Read file with FileReader and strip data-URL prefix. Use generation counter + AbortController so replacing/removing file cannot apply stale responses. Login/offline changes must not leave a stale enabled submit button. Display inspected page count and fixed settings before enabling submission.

`POST /api/print/jobs` body `{pdf:<base64>,password:<school password>,inspection_token:<token>,idempotency_key:<crypto.randomUUID()>}`. Server returns HTTP 200/202/503 etc with `{job:<job>}` for tracked results; validation failures return `{error:"Chinese message",code:"machine_code"}`. A job is:
```
{"id":"opaque","idempotency_key":"opaque","state":"processing|submitted|unknown|failed|rejected","pages":2,"created_at":1790000000,"updated_at":1790000000,"code":"submitted","message":"已交给学校队列，请刷卡取件。"}
```
Generate one idempotency key per explicit intended submission and keep it through a network failure. No automatic retry of POST. For an ambiguous network failure show 查询任务状态, fetch history and match idempotency_key. If result remains unknown, tell user to check printer queue. Do not create a new key or auto-resend on fetch failure. Definite failed/rejected results allow user to explicitly re-enter password and start a new attempt. Disable repeat clicks immediately. If submit is in progress, prevent file replacement, login changes and a second submission.

`GET /api/print/jobs` returns `{jobs:[job,...]}` latest owned jobs only. `GET /api/print/jobs/<id>` returns `{job:job}` and may reconcile uncertain status. Poll only active processing/unknown tasks with capped polling; leave manual refresh. Render all output safely. Display a short job ID users can recognize and UTC timestamps in local time. API retains metadata 24h, never document or password; explain this in concise help/privacy copy.

Backend errors: 401 sign in; 403 identity/CSRF mismatch refresh session; 413 too large; 422 invalid/encrypted/too-many-pages PDF; 429 rate limit; 503 service offline/busy. User's chosen file can remain in memory for recoverable pre-submit errors. Network exception is not success. Do not fake backend results or add demo mode in production frontend.

Read this brief, implement complete polished frontend and a short DESIGN.md describing design decisions. Check JS syntax with node --check. Provide a concise final list of created files. Do not wait for the engineer's backend to start.
