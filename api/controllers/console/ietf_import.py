"""Console endpoints for importing IETF mailarchive and RFC content.

These endpoints delegate data extraction to the external `ietfdata` sidecar
service, then persist the generated dataset file as a normal uploaded document.
The controller layer remains intentionally thin: input validation happens via
Pydantic models and the upload orchestration lives in `IetfImportService`.
"""

from __future__ import annotations

from typing import Literal

from flask import request
from flask_restx import Resource
from pydantic import BaseModel, ConfigDict, Field, PositiveInt, field_validator, model_validator

from controllers.console import console_ns
from libs.login import login_required
from services.ietf_import_service import IetfImportService


class IetfMailarchiveImportRequest(BaseModel):
    """Input payload for importing messages from a single IETF mailing list."""

    mailing_list: str = Field(..., min_length=1, description="Mailing list name such as 'httpapi'")
    sent_after: str = Field(default="1970-01-01T00:00:00", description="Lower bound for message sent date")
    sent_before: str = Field(default="2038-01-19T03:14:07", description="Upper bound for message sent date")
    from_addr: str | None = Field(default=None, description="Optional sender email substring filter")
    subject: str | None = Field(default=None, description="Optional message subject substring filter")
    offset: int = Field(default=0, ge=0, description="Number of matched messages to skip before collecting results")
    limit: PositiveInt = Field(default=100, description="Maximum number of messages to include when fetch_all is false")
    output_format: Literal["mbox_text", "jsonl"] = Field(
        default="jsonl",
        description="Export as structured JSONL (one record per email) or raw mbox-style plain text",
    )
    fetch_all: bool = Field(default=False, description="If true, include all matched messages in one import")
    refresh: bool = Field(default=False, description="Refresh the mailing list from the upstream archive before query")

    model_config = ConfigDict(extra="forbid")


class IetfRfcImportRequest(BaseModel):
    """Input payload for importing one or more RFC documents as markdown."""

    rfc_ids: list[str] = Field(default_factory=list, description="Explicit RFC identifiers such as RFC9000")
    since: str | None = Field(default=None, description="Lower month bound in YYYY-MM format for filtered RFC queries")
    until: str | None = Field(default=None, description="Upper month bound in YYYY-MM format for filtered RFC queries")
    stream: str | None = Field(default=None, description="Optional RFC stream filter, for example IETF or IRTF")
    wg: str | None = Field(default=None, description="Optional working group acronym filter")
    status: str | None = Field(default=None, description="Optional current status filter")
    limit: PositiveInt = Field(default=20, le=100, description="Maximum number of RFCs to include for filtered queries")

    model_config = ConfigDict(extra="forbid")

    @model_validator(mode="after")
    def validate_query_shape(self) -> IetfRfcImportRequest:
        if self.rfc_ids:
            return self
        if not any((self.since, self.until, self.stream, self.wg, self.status)):
            raise ValueError("Either rfc_ids or at least one RFC filter must be provided")
        return self

    @field_validator("rfc_ids", mode="before")
    @classmethod
    def normalize_rfc_ids(cls, value: object) -> list[str]:
        if not isinstance(value, list):
            return []

        normalized: list[str] = []
        seen: set[str] = set()
        for item in value:
            text = str(item).strip()
            if not text:
                continue
            upper = text.upper()
            if upper.isdigit():
                upper = f"RFC{upper}"
            elif upper.startswith("RFC") and upper[3:].isdigit():
                upper = f"RFC{upper[3:]}"

            key = upper.lower()
            if key in seen:
                continue
            seen.add(key)
            normalized.append(upper)

        return normalized


class IetfRfcSuggestionQuery(BaseModel):
    query: str = Field(default="")
    limit: PositiveInt = Field(default=20, le=100)

    model_config = ConfigDict(extra="forbid")


class IetfDraftImportRequest(BaseModel):
    draft_names: list[str] = Field(
        default_factory=list,
        description="Full IETF draft names, e.g. draft-ietf-quic-transport-34",
    )

    model_config = ConfigDict(extra="forbid")

    @field_validator("draft_names", mode="before")
    @classmethod
    def validate_draft_names(cls, value: object) -> list[str]:
        if not isinstance(value, list):
            return []
        return [str(item).strip() for item in value if str(item).strip()]


@console_ns.route("/ietf/imports/mailarchive")
class IetfMailarchiveImportApi(Resource):
    @login_required
    def post(self):
        payload = IetfMailarchiveImportRequest.model_validate(console_ns.payload)
        return IetfImportService.import_mailarchive(payload.model_dump())


@console_ns.route("/ietf/imports/mailarchive/mbox")
class IetfMailarchiveMboxImportApi(Resource):
    @login_required
    def post(self):
        payload = IetfMailarchiveImportRequest.model_validate(console_ns.payload)
        return IetfImportService.import_mailarchive_as_mbox(payload.model_dump())


@console_ns.route("/ietf/imports/mailarchive/mbox/download")
class IetfMailarchiveMboxDownloadApi(Resource):
    @login_required
    def post(self):
        payload = IetfMailarchiveImportRequest.model_validate(console_ns.payload)
        return IetfImportService.download_mailarchive_as_mbox_to_local(payload.model_dump())


class IetfMailarchivePreprocessRequest(BaseModel):
    """Input payload for downloading and preprocessing mbox files per calendar year."""

    mailing_list: str = Field(..., min_length=1)
    sent_after: str = Field(default="1970-01-01T00:00:00")
    sent_before: str = Field(default="2038-01-19T03:14:07")
    from_addr: str | None = Field(default=None)
    subject: str | None = Field(default=None)
    refresh: bool = Field(default=False)

    model_config = ConfigDict(extra="forbid")


class IetfPreprocessedUploadRequest(BaseModel):
    """Input payload for uploading a preprocessed local IETF file."""

    processed_path: str = Field(..., min_length=1)

    model_config = ConfigDict(extra="forbid")


@console_ns.route("/ietf/imports/mailarchive/mbox/preprocess")
class IetfMailarchiveMboxPreprocessApi(Resource):
    @login_required
    def post(self):
        payload = IetfMailarchivePreprocessRequest.model_validate(console_ns.payload)
        return IetfImportService.download_and_preprocess_years(
            mailing_list=payload.mailing_list,
            sent_after=payload.sent_after,
            sent_before=payload.sent_before,
            from_addr=payload.from_addr,
            subject=payload.subject,
            refresh=payload.refresh,
        )


@console_ns.route("/ietf/imports/mailarchive/preprocessed/upload")
class IetfMailarchivePreprocessedUploadApi(Resource):
    @login_required
    def post(self):
        payload = IetfPreprocessedUploadRequest.model_validate(console_ns.payload)
        return IetfImportService.upload_preprocessed_file(payload.processed_path)


@console_ns.route("/ietf/imports/rfcs")
class IetfRfcImportApi(Resource):
    @login_required
    def post(self):
        payload = IetfRfcImportRequest.model_validate(console_ns.payload)
        return IetfImportService.import_rfcs(payload.model_dump())


@console_ns.route("/ietf/imports/drafts")
class IetfDraftImportApi(Resource):
    @login_required
    def post(self):
        payload = IetfDraftImportRequest.model_validate(console_ns.payload)
        return IetfImportService.import_drafts({"draft_names": payload.draft_names})


@console_ns.route("/ietf/mailarchive/lists")
class IetfMailingListsApi(Resource):
    @login_required
    def get(self):
        lists = IetfImportService.get_mailing_lists()
        return {"lists": lists}


@console_ns.route("/ietf/rfcs/suggestions")
class IetfRfcSuggestionsApi(Resource):
    @login_required
    def get(self):
        query = IetfRfcSuggestionQuery.model_validate(request.args.to_dict(flat=True))  # type: ignore[arg-type]
        suggestions = IetfImportService.get_rfc_suggestions(query=query.query, limit=query.limit)
        return {"suggestions": suggestions}