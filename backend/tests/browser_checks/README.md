# Browser regression checks

These pytest checks replace the temporary `verify_*.py` scripts. They use the existing
Playwright development dependency, automatic browser lifecycle management, locator waits,
and fresh isolated contexts. They do not launch on import or use fixed debugging ports,
machine-specific Chrome paths, or historical report IDs.

Run from `backend/` after starting the Vite dev frontend. Install the locked dependencies
with `uv sync --frozen --python 3.12`. Use an installed Chrome via `AE_CHROME_PATH`, or install
Playwright Chromium with `uv run playwright install chromium`.

```powershell
$env:AE_BROWSER_TEST = '1'
$env:AE_BROWSER_URL = 'http://127.0.0.1:5173'
# Optional: point to an existing Chrome executable on this machine.
# $env:AE_CHROME_PATH = '<absolute executable path>'
uv run pytest -q tests/browser_checks/test_mocked_flows.py

# Backend, PostgreSQL, object storage and current migrations are required for live reads.
$env:AE_BROWSER_LIVE_TEST = '1'
uv run pytest -q tests/browser_checks
```

| Check | Data | Coverage |
| --- | --- | --- |
| Preflight | Mock JavaScript fixture | Failed resource blocks submission, explicit confirmation, changed selection invalidates |
| Report charts | Mock JavaScript fixture | Matched baseline, genuine zero, missing baseline, partial coverage, unavailable evidence |
| Report analysis | Mock JavaScript fixture | Independent start route, all Demo state labels, and no start action for non-real reports |
| Detail navigation | Read-only local API | Discover a report, task overview/dataset/cases/snapshot, report drilldown and return |
| Workbench | Read-only local API | Dev menu, input form, unavailable evidence blocks nomination, import template, diagnostics and board |
| Standard inputs | Intercepted API fixtures | Preview, differences, import, model/target mapping, compilation drawer, stale-result invalidation; no real writes |

Both flags default to off; report navigation skips if no report exists. It never creates
an evaluation to populate data. The live workbench check requires a development frontend.
Browser requests outside the configured local origin and all real write requests are blocked.
A blocked write fails teardown. API GET discovery also uses that local origin. No approve,
import, diagnostic submission, model generation, or real analysis start/retry request is made.
Mocked analysis checks may click start only when the JavaScript fixture intercepts the request.

Screenshots (including failure state) go to `.screenshots/browser-checks/` in the repository
root, or `AE_BROWSER_ARTIFACT_DIR`. Browser profiles are ephemeral. Generated screenshots,
reports and local editor/share files are ignored; JavaScript fixtures and reusable tests are
tracked. Fixtures are synthetic UI inputs, never evidence of real scores or resource access.

Mock checks do not replace SSH, model correctness, object durability, or real-report acceptance.
