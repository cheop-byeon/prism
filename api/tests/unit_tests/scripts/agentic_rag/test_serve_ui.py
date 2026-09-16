import pytest

from scripts.agentic_rag.serve_ui import _is_data_admin_route


class TestIsDataAdminRoute:
    @pytest.mark.parametrize(
        "path",
        [
            "/api/corpora/mail-sync",
            "/api/corpora/draft-download",
            "/api/corpora/draft-download-all",
            "/api/corpora/local-sync",
            "/api/corpora/local-index",
            "/api/corpora/topic-pull",
            "/api/sources/dnsop/agenda/download",
            "/api/sources/ledbat/minutes/download",
            "/api/observability",
        ],
    )
    def test_admin_only_actions_require_admin(self, path):
        # Mostly mutating data-sync actions, plus /api/observability -- a
        # read-only route still gated the same way since it's org-wide
        # across every account, not one account's own data.
        assert _is_data_admin_route(path) is True

    @pytest.mark.parametrize(
        "path",
        [
            "/api/corpora",
            "/api/corpora/freshness",
            "/api/corpora/rfc-suggestions",
            "/api/corpora/mailing-lists",
            "/api/corpora/wg-documents",
            "/api/corpora/topic-discover",
            "/api/sources/types",
            "/api/sources/dnsop/agenda",
            "/api/ask",
            "/api/deep-dive",
            "/api/panorama",
            "/api/sessions",
            "/api/models",
            "/api/my-llm-credentials",
            "/api/my-llm-credentials/langgenius/openai/openai",
            "/",
            "/login",
        ],
    )
    def test_read_only_and_research_routes_stay_open(self, path):
        # A visitor's own personal Dify tenant (from self-service sign-up)
        # must never gate these -- Research/Sessions/Feedback and read-only
        # browsing are open to any logged-in account by design. BYOK
        # credentials are account-scoped, not tenant-wide, so managing
        # your own key is exactly as open as everything else here.
        assert _is_data_admin_route(path) is False

    def test_download_suffix_alone_is_not_enough(self):
        # Guards against a too-loose match -- only paths actually under
        # /api/sources/ should be caught by the "/download" suffix rule.
        assert _is_data_admin_route("/api/other/download") is False
