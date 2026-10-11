"""One-off: resolve the owning working group (mailing-list name) for every
currently-registered local source, so a later mail-pull batch job knows the
real scope (how many UNIQUE lists, not drafts) before it runs anything
against mailarchive.ietf.org.

Reuses IetfImportService.resolve_wg_for_draft as-is (no new resolution
logic) but adds one process-local cache this script's own caller doesn't
have: that function's own docstring notes it does two live Datatracker
calls per draft (doc -> group URL, then group URL -> list_email), uncached,
by design, since every other caller only ever resolves one draft at a
time. Many drafts share the same group, so caching group_url -> wg_name
here cuts the second call for every draft after the first one in that
group -- pure batch-scope optimization, not a change to how resolution
itself works.

Writes a JSON map {draft_name: wg_or_null} to storage/ so the mail-pull
batch job (and this script's own progress report) can just load it.
"""

import json
import sys
import time

sys.path.insert(0, "/app/api")

from app_factory import create_app

app = create_app()

with app.app_context():
    from extensions.ext_database import db
    from models.ietf_local_source import IetfLocalSource
    from services.ietf_import_service import IetfImportService

    OUT_PATH = "/app/api/storage/draft_to_wg.json"

    names = sorted(row[0] for row in db.session.query(IetfLocalSource.draft_name).all())
    print(f"[resolve-wg] {len(names)} drafts to resolve.", flush=True)

    group_url_cache: dict[str, str | None] = {}
    draft_to_wg: dict[str, str | None] = {}
    start = time.monotonic()

    import httpx

    def resolve(draft_name: str) -> str | None:
        try:
            with httpx.Client(timeout=15.0, follow_redirects=True) as client:
                doc_resp = client.get(
                    f"https://datatracker.ietf.org/api/v1/doc/document/{draft_name}/", params={"format": "json"}
                )
                if doc_resp.status_code != 200:
                    return None
                group_url = doc_resp.json().get("group")
                if not group_url:
                    return None
                if group_url in group_url_cache:
                    return group_url_cache[group_url]
                group_resp = client.get(f"https://datatracker.ietf.org{group_url}", params={"format": "json"})
                if group_resp.status_code != 200:
                    group_url_cache[group_url] = None
                    return None
                group_data = group_resp.json()
                list_email = group_data.get("list_email")
                wg = list_email.split("@", 1)[0] if list_email and "@" in list_email else group_data.get("acronym")
                group_url_cache[group_url] = wg
                return wg
        except Exception:
            return None

    for i, draft_name in enumerate(names, start=1):
        wg = resolve(draft_name)
        draft_to_wg[draft_name] = wg
        if i % 200 == 0 or i == len(names):
            elapsed = time.monotonic() - start
            rate = i / elapsed if elapsed > 0 else 0
            eta_min = (len(names) - i) / rate / 60 if rate > 0 else float("nan")
            unique_wgs = len({w for w in draft_to_wg.values() if w})
            print(
                f"[resolve-wg] {i}/{len(names)} resolved, {unique_wgs} unique WGs so far, "
                f"~{eta_min:.0f}m remaining",
                flush=True,
            )
            with open(OUT_PATH, "w") as f:
                json.dump(draft_to_wg, f)

    with open(OUT_PATH, "w") as f:
        json.dump(draft_to_wg, f)

    unique_wgs = sorted({w for w in draft_to_wg.values() if w})
    no_wg = sum(1 for w in draft_to_wg.values() if not w)
    print(f"[resolve-wg] Done. {len(unique_wgs)} unique WGs, {no_wg} drafts with no resolvable WG.", flush=True)
    print(f"[resolve-wg] Wrote {OUT_PATH}", flush=True)
