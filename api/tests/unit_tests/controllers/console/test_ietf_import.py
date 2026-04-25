from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from controllers.console import ietf_import as ietf_import_module


def _unwrap(func):
    while hasattr(func, "__wrapped__"):
        func = func.__wrapped__
    return func


def test_mailarchive_import_delegates_to_service(app, monkeypatch: pytest.MonkeyPatch) -> None:
    api = ietf_import_module.IetfMailarchiveImportApi()
    handler = _unwrap(api.post)
    service_mock = MagicMock(return_value=({"id": "file-1"}, 201))
    monkeypatch.setattr(ietf_import_module.IetfImportService, "import_mailarchive", service_mock)

    with app.test_request_context(
        method="POST",
        json={
            "mailing_list": "httpapi",
            "sent_after": "2024-01-01T00:00:00",
            "sent_before": "2024-02-01T00:00:00",
            "subject": "charter",
            "limit": 25,
        },
    ):
        payload, status = handler(api)

    assert status == 201
    assert payload["id"] == "file-1"
    service_mock.assert_called_once_with(
        {
            "mailing_list": "httpapi",
            "sent_after": "2024-01-01T00:00:00",
            "sent_before": "2024-02-01T00:00:00",
            "from_addr": None,
            "subject": "charter",
            "limit": 25,
            "refresh": False,
        }
    )


def test_rfc_import_requires_ids_or_filters(app) -> None:
    api = ietf_import_module.IetfRfcImportApi()
    handler = _unwrap(api.post)

    with app.test_request_context(method="POST", json={"rfc_ids": [], "limit": 5}):
        with pytest.raises(Exception, match="Either rfc_ids or at least one RFC filter"):
            handler(api)


def test_rfc_import_delegates_to_service(app, monkeypatch: pytest.MonkeyPatch) -> None:
    api = ietf_import_module.IetfRfcImportApi()
    handler = _unwrap(api.post)
    service_mock = MagicMock(return_value=({"id": "file-rfc"}, 201))
    monkeypatch.setattr(ietf_import_module.IetfImportService, "import_rfcs", service_mock)

    with app.test_request_context(
        method="POST",
        json={
            "rfc_ids": ["RFC9000", "RFC9114"],
            "limit": 2,
        },
    ):
        payload, status = handler(api)

    assert status == 201
    assert payload["id"] == "file-rfc"
    service_mock.assert_called_once_with(
        {
            "rfc_ids": ["RFC9000", "RFC9114"],
            "since": None,
            "until": None,
            "stream": None,
            "wg": None,
            "status": None,
            "limit": 2,
        }
    )