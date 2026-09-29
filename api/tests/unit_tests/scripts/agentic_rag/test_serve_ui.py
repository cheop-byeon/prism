import pytest

from scripts.agentic_rag.serve_ui import _is_data_admin_route


class TestIsDataAdminRoute:
    def test_observability_is_the_only_admin_only_route(self):
        # Every other former admin-only action (mail-sync, draft-download,
        # local-sync, local-index, topic-pull, sources/.../download) is now
        # gated by real per-source ownership instead (IetfLocalSource.
        # owner_account_id / Dataset.created_by) -- /api/observability is
        # org-wide across every account, not one account's own data, so it's
        # the one route still restricted here.
        assert _is_data_admin_route("/api/observability") is True

    @pytest.mark.parametrize(
        "path",
        [
            "/api/corpora",
            "/api/corpora/freshness",
            "/api/corpora/rfc-suggestions",
            "/api/corpora/mailing-lists",
            "/api/corpora/wg-documents",
            "/api/corpora/topic-discover",
            "/api/corpora/mail-sync",
            "/api/corpora/draft-download",
            "/api/corpora/draft-download-all",
            "/api/corpora/local-sync",
            "/api/corpora/local-index",
            "/api/corpora/local-embedding-index",
            "/api/corpora/local-share",
            "/api/corpora/topic-pull",
            "/api/sources/types",
            "/api/sources/dnsop/agenda",
            "/api/sources/dnsop/agenda/download",
            "/api/ask",
            "/api/deep-dive",
            "/api/panorama",
            "/api/sessions",
            "/api/models",
            "/",
            "/login",
        ],
    )
    def test_everything_else_stays_open(self, path):
        # A visitor's own personal Dify tenant (from self-service sign-up)
        # must never gate these -- any logged-in account may pull/register
        # its own sources now, own what it pulls, and choose to share it;
        # Research/Sessions/Feedback and read-only browsing were already
        # open to any logged-in account by design.
        assert _is_data_admin_route(path) is False
