"""OpenAPI ↔ API Contract v1 consistency.

The contract is only worth having if the implementation cannot quietly drift
from it. These checks read the generated OpenAPI document, so they fail the
moment a route, an enum or a forbidden field diverges. No database and no model
are needed.
"""

from __future__ import annotations

import os

import pytest

os.environ.setdefault("APP_ENV", "test")
os.environ.setdefault("SHARED_ROOT", "/tmp")
os.environ.setdefault("DATABASE_URL", "postgresql://unused/unused")

from api.app import API_PREFIX, create_app  # noqa: E402
from api.errors import ERROR_CODES  # noqa: E402

#: Exactly the Search / Document scope of API Contract v1.
EXPECTED_ROUTES = {
    ("GET", f"{API_PREFIX}/search"),
    ("GET", f"{API_PREFIX}/documents/{{document_id}}"),
    ("GET", f"{API_PREFIX}/documents/{{document_id}}/revisions"),
    ("GET", f"{API_PREFIX}/documents/{{document_id}}/download"),
    ("GET", f"{API_PREFIX}/tags"),
    ("GET", f"{API_PREFIX}/departments"),
}

#: Never acceptable anywhere in a response schema.
FORBIDDEN_FIELDS = {
    "score", "retrieval_score", "similarity", "confidence", "relevance",
    "rrf_score", "distance",
    "source_path", "source_path_at_ingest", "original_filename",
    "extracted_text", "parsed_structure", "embedding", "search_vector",
    "error_message", "parser_name", "parser_version", "chunking_version",
    "embedding_status", "summary_status", "tagging_status",
    "absolute_path", "shared_root",
}


@pytest.fixture(scope="module")
def spec():
    return create_app().openapi()


def response_schemas(spec) -> dict:
    return spec.get("components", {}).get("schemas", {})


class TestRoutes:
    def test_route_set_matches_the_contract_scope(self, spec):
        actual = {
            (method.upper(), path)
            for path, ops in spec["paths"].items()
            for method in ops
            if method.upper() in {"GET", "POST", "PUT", "PATCH", "DELETE"}
        }
        assert actual == EXPECTED_ROUTES

    def test_no_write_methods_exist(self, spec):
        """The shared folder is the source of truth; the API never modifies it."""
        for path, ops in spec["paths"].items():
            for method in ops:
                assert method.upper() not in {"POST", "PUT", "PATCH", "DELETE"}, (
                    f"{method.upper()} {path} must not exist"
                )

    def test_document_delete_route_is_absent(self, spec):
        assert "delete" not in spec["paths"].get(f"{API_PREFIX}/documents/{{document_id}}", {})

    def test_chat_routes_are_not_stubbed(self, spec):
        """Contract defines Chat, but a 501 stub would misrepresent readiness."""
        assert not [p for p in spec["paths"] if "/chat" in p]

    def test_every_route_is_under_the_v1_prefix(self, spec):
        assert all(path.startswith(API_PREFIX) for path in spec["paths"])


class TestSchemas:
    def test_no_forbidden_field_appears_in_any_schema(self, spec):
        offenders = []
        for name, schema in response_schemas(spec).items():
            for field in schema.get("properties", {}):
                if field in FORBIDDEN_FIELDS:
                    offenders.append(f"{name}.{field}")
        assert offenders == [], f"forbidden fields exposed: {offenders}"

    def test_search_item_has_the_contract_fields(self, spec):
        properties = set(response_schemas(spec)["SearchItemOut"]["properties"])
        assert {
            "document_id", "title", "file_type", "department", "tags",
            "updated_at", "current_revision", "has_newer_revision",
            "snippet", "matched_chunk",
        } <= properties

    def test_pagination_envelope_on_list_responses(self, spec):
        schemas = response_schemas(spec)
        for name in ("SearchResponse", "RevisionListResponse", "TagListResponse"):
            assert {"items", "page", "size", "total"} <= set(schemas[name]["properties"])

    def test_departments_response_has_no_pagination(self, spec):
        """Contract section 11's documented exception."""
        properties = set(response_schemas(spec)["DepartmentListResponse"]["properties"])
        assert properties == {"items"}

    def test_revision_schema_matches_the_contract(self, spec):
        properties = set(response_schemas(spec)["RevisionOut"]["properties"])
        assert {
            "revision_id", "revision_no", "content_hash", "file_size",
            "source_modified_at", "parse_status", "parse_result_code",
            "is_current", "is_ready", "created_at",
        } <= properties

    def test_anchor_is_a_discriminated_union(self, spec):
        schemas = response_schemas(spec)
        for name, expected in [
            ("ParagraphAnchor", "paragraph"), ("PageAnchor", "page"), ("NoAnchor", "none"),
        ]:
            assert schemas[name]["properties"]["type"]["const"] == expected

    def test_page_anchor_requires_a_positive_page(self, spec):
        assert response_schemas(spec)["PageAnchor"]["properties"]["page_number"]["minimum"] == 1


class TestErrorContract:
    def test_error_codes_match_the_contract_set(self):
        assert set(ERROR_CODES) == {
            "UNAUTHENTICATED", "FORBIDDEN", "VALIDATION_ERROR", "BAD_REQUEST",
            "DOCUMENT_NOT_FOUND", "REVISION_NOT_FOUND", "DOCUMENT_NOT_DOWNLOADABLE",
            "CHAT_SESSION_NOT_FOUND", "CHAT_MESSAGE_TOO_LONG", "SEARCH_QUERY_TOO_LONG",
            "RATE_LIMITED", "INTERNAL_ERROR",
        }

    def test_each_code_maps_to_its_documented_status(self):
        assert ERROR_CODES["DOCUMENT_NOT_FOUND"] == 404
        assert ERROR_CODES["DOCUMENT_NOT_DOWNLOADABLE"] == 409
        assert ERROR_CODES["VALIDATION_ERROR"] == 422
        assert ERROR_CODES["SEARCH_QUERY_TOO_LONG"] == 422
        assert ERROR_CODES["UNAUTHENTICATED"] == 401

    def test_unknown_code_cannot_be_constructed(self):
        from api.errors import ApiError

        with pytest.raises(ValueError):
            ApiError("NOT_IN_THE_SET", "…")


class TestSearchParameters:
    def test_search_accepts_exactly_the_contract_parameters(self, spec):
        params = {
            p["name"] for p in spec["paths"][f"{API_PREFIX}/search"]["get"]["parameters"]
            if p["in"] == "query"
        }
        assert params == {"q", "mode", "page", "size", "department_id", "year",
                          "tag_id", "file_type"}

    def test_size_bounds_are_declared(self, spec):
        for p in spec["paths"][f"{API_PREFIX}/search"]["get"]["parameters"]:
            if p["name"] == "size":
                schema = p["schema"]
                assert schema.get("minimum") == 1 and schema.get("maximum") == 100
                return
        pytest.fail("size parameter not declared")
