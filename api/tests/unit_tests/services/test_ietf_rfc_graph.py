"""Unit tests for RFC relationship graph completeness reporting."""

from services.ietf_import_service import IetfImportService


class TestBuildRfcRelationshipGraph:
    def test_surfaces_family_lookup_error_and_truncation_note(self, monkeypatch):
        monkeypatch.setattr(
            IetfImportService,
            "_structural_neighbors",
            lambda rfc: {
                "center": "RFC100",
                "nodes": [{"id": "RFC100", "title": "Root"}],
                "edges": [],
            },
        )
        monkeypatch.setattr(
            IetfImportService,
            "find_related_rfc_family",
            lambda rfc: {
                "error": "Datatracker unavailable",
                "note": "Citation results were capped.",
                "family": [],
            },
        )
        monkeypatch.setattr(IetfImportService, "_batch_fetch_rfc_nodes", lambda names: [{"id": "RFC100"}])

        from services.rfc_corpus_index_service import RfcCorpusIndexService

        monkeypatch.setattr(RfcCorpusIndexService, "is_title_abstract_corpus_built", lambda: False)

        result = IetfImportService.build_rfc_relationship_graph("100")

        assert result["warnings"] == [
            "Could not load citation-family relationships: Datatracker unavailable",
            "Citation results were capped.",
        ]


class TestBuildFocusNeighborhood:
    def test_reports_failed_branch_and_keeps_successful_branch(self, monkeypatch):
        monkeypatch.setattr(
            IetfImportService,
            "build_rfc_relationship_graph",
            lambda rfc: {
                "center": "RFC100",
                "nodes": [{"id": "RFC100"}, {"id": "RFC101"}, {"id": "RFC102"}],
                "edges": [
                    {"source": "RFC100", "target": "RFC101", "kind": "updates"},
                    {"source": "RFC100", "target": "RFC102", "kind": "obsoletes"},
                ],
            },
        )

        def structural_neighbors(rfc):
            if rfc == "RFC101":
                return {"error": "Datatracker unavailable"}
            return {
                "center": "RFC102",
                "nodes": [{"id": "RFC102"}, {"id": "RFC103"}],
                "edges": [{"source": "RFC102", "target": "RFC103", "kind": "updates"}],
            }

        monkeypatch.setattr(IetfImportService, "_structural_neighbors", structural_neighbors)

        result = IetfImportService.build_focus_neighborhood("100", max_hops=2)

        assert {node["id"] for node in result["nodes"]} == {"RFC100", "RFC101", "RFC102", "RFC103"}
        assert result["warnings"] == ["Could not expand RFC101: Datatracker unavailable"]
