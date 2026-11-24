from types import SimpleNamespace

import pytest

from scripts.agentic_rag.serve_ui import (
    _SPECTRUM_PORT,
    IndexPage,
    _is_data_admin_route,
    _render_spectrum_page,
    _resolve_index_page,
    _spectrum_home_url,
)


class TestResolveIndexPage:
    @pytest.mark.parametrize("is_data_admin", [True, False])
    def test_spectrum_port_serves_spectrum_to_everyone(self, is_data_admin):
        assert _resolve_index_page(str(_SPECTRUM_PORT), is_data_admin) is IndexPage.SPECTRUM

    def test_classic_port_serves_classic_to_admins(self):
        assert _resolve_index_page("8765", is_data_admin=True) is IndexPage.CLASSIC

    def test_classic_port_refuses_non_admins(self):
        assert _resolve_index_page("8765", is_data_admin=False) is IndexPage.CLASSIC_FORBIDDEN

    def test_missing_port_is_treated_as_classic(self):
        assert _resolve_index_page(None, is_data_admin=False) is IndexPage.CLASSIC_FORBIDDEN


class TestSpectrumHomeUrl:
    def test_behind_nginx_uses_the_public_spectrum_path(self):
        assert _spectrum_home_url("/prism", "https", "prism.uiocloud.no") == "/spectrum/"

    def test_local_dev_points_at_the_spectrum_port(self):
        assert _spectrum_home_url("", "http", "localhost") == f"http://localhost:{_SPECTRUM_PORT}/"


class TestRenderSpectrumPageAdminOnlyChrome:
    _USERS_BUTTON = '<button data-goto="users"'
    _CLASSIC_LINK = '<a class="po-classiclink" id="poClassicLink"'

    @staticmethod
    def _opening_tag(html: str, start: str) -> str:
        begin = html.index(start)
        return html[begin : html.index(">", begin) + 1]

    def _render(self, is_data_admin: bool) -> str:
        account = SimpleNamespace(email="reader@example.org")
        return _render_spectrum_page(account, is_data_admin=is_data_admin)

    def test_non_admin_gets_users_nav_and_classic_link_hidden(self):
        html = self._render(is_data_admin=False)

        assert " hidden" in self._opening_tag(html, self._USERS_BUTTON)
        assert " hidden" in self._opening_tag(html, self._CLASSIC_LINK)
        assert "const IS_DATA_ADMIN = false;" in html

    def test_admin_sees_users_nav_and_classic_link(self):
        html = self._render(is_data_admin=True)

        assert " hidden" not in self._opening_tag(html, self._USERS_BUTTON)
        assert " hidden" not in self._opening_tag(html, self._CLASSIC_LINK)
        assert "const IS_DATA_ADMIN = true;" in html

    @pytest.mark.parametrize("is_data_admin", [True, False])
    def test_no_placeholder_is_left_unfilled(self, is_data_admin):
        html = self._render(is_data_admin=is_data_admin)

        assert "__ADMIN_ONLY_HIDDEN__" not in html
        assert "__IS_DATA_ADMIN__" not in html


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
