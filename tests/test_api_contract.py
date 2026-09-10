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

#: Exactly the eleven method/path pairs in API Contract v1.1 -- v1's ten plus
#: the additive GET /folders.
EXPECTED_ROUTES = {
    ("GET", f"{API_PREFIX}/folders"),
    ("GET", f"{API_PREFIX}/search"),
    ("GET", f"{API_PREFIX}/documents/{{document_id}}"),
    ("GET", f"{API_PREFIX}/documents/{{document_id}}/revisions"),
    ("GET", f"{API_PREFIX}/documents/{{document_id}}/download"),
    ("GET", f"{API_PREFIX}/tags"),
    ("GET", f"{API_PREFIX}/departments"),
    ("POST", f"{API_PREFIX}/chat/sessions"),
    ("GET", f"{API_PREFIX}/chat/sessions"),
    ("GET", f"{API_PREFIX}/chat/sessions/{{session_id}}"),
    ("POST", f"{API_PREFIX}/chat/sessions/{{session_id}}/messages"),
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

    def test_only_chat_writes_exist(self, spec):
        """Chat persistence cannot introduce document/source write endpoints."""
        for path, ops in spec["paths"].items():
            for method in ops:
                assert method.upper() not in {"PUT", "PATCH", "DELETE"}, (
                    f"{method.upper()} {path} must not exist"
                )
                if method.upper() == 'POST':
                    assert path.startswith(f'{API_PREFIX}/chat/sessions')

    def test_document_delete_route_is_absent(self, spec):
        assert "delete" not in spec["paths"].get(f"{API_PREFIX}/documents/{{document_id}}", {})

    def test_chat_routes_are_not_stubbed(self, spec):
        for path, ops in spec['paths'].items():
            if '/chat/' in path:
                for operation in ops.values():
                    assert '501' not in operation['responses']

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
        # v1's eight, plus folder_path from the v1.1 additive revision.
        assert params == {"q", "mode", "page", "size", "department_id", "year",
                          "tag_id", "file_type", "folder_path"}

    def test_size_bounds_are_declared(self, spec):
        for p in spec["paths"][f"{API_PREFIX}/search"]["get"]["parameters"]:
            if p["name"] == "size":
                schema = p["schema"]
                assert schema.get("minimum") == 1 and schema.get("maximum") == 100
                return
        pytest.fail("size parameter not declared")


class TestChatContract:
    def test_request_shapes_forbid_client_identity(self, spec):
        schemas = response_schemas(spec)
        for name, fields in [('CreateSessionRequest', {'title'}), ('SendMessageRequest', {'message'})]:
            assert set(schemas[name]['properties']) == fields
            assert schemas[name]['additionalProperties'] is False
        assert 'required' not in schemas['CreateSessionRequest']
        message = schemas['SendMessageRequest']['properties']['message']
        assert message['minLength'] == 1 and message['maxLength'] == 4000

    def test_response_shapes_match_contract(self, spec):
        schemas = response_schemas(spec)
        session = {'session_id', 'title', 'created_at', 'updated_at'}
        assert set(schemas['SessionOut']['properties']) == session
        assert set(schemas['SessionDetail']['properties']) == session | {'messages'}
        assert set(schemas['SessionListItem']['properties']) == session | {'message_count'}
        assert set(schemas['SendMessageResponse']['properties']) == {
            'message_id', 'answer', 'refused', 'sources', 'created_at',
        }
        assert schemas['SendMessageResponse']['properties']['refused']['type'] == 'boolean'
        assert schemas['AssistantMessage']['properties']['refused']['type'] == 'boolean'
        assert schemas['AssistantMessage']['properties']['content_hidden']['type'] == 'boolean'

    def test_sources_omit_inaccessible_metadata_and_reuse_anchor(self, spec):
        schemas = response_schemas(spec)
        ids = {'document_id', 'revision_id', 'chunk_id', 'accessible'}
        assert set(schemas['InaccessibleSource']['properties']) == ids
        assert set(schemas['AccessibleSource']['properties']) == ids | {
            'title', 'file_type', 'section_title', 'anchor',
        }
        anchor = schemas['AccessibleSource']['properties']['anchor']
        assert anchor['discriminator']['propertyName'] == 'type'
        assert set(anchor['discriminator']['mapping']) == {'paragraph', 'page', 'none'}

    def test_status_codes_pagination_and_common_error_schemas(self, spec):
        for path, methods in spec['paths'].items():
            if '/chat/' not in path:
                continue
            for method, operation in methods.items():
                assert ('201' if method == 'post' else '200') in operation['responses']
                for status in ('401', '404', '422', '500'):
                    schema = operation['responses'][status]['content']['application/json']['schema']
                    assert schema['$ref'].endswith('/ErrorResponse')
        for path, default in [('/api/v1/chat/sessions', 20), ('/api/v1/chat/sessions/{session_id}', 50)]:
            params = spec['paths'][path]['get']['parameters']
            size = next(p['schema'] for p in params if p['name'] == 'size')
            assert size['default'] == default and size['maximum'] == 100

    def test_rate_limited_is_declared_only_where_a_provider_is_reached(self, spec):
        """RATE_LIMITED is an existing contract code, not a new one."""
        assert ERROR_CODES["RATE_LIMITED"] == 429
        messages = f'{API_PREFIX}/chat/sessions/{{session_id}}/messages'
        for path, methods in spec['paths'].items():
            if '/chat/' not in path and not path.endswith('/chat/sessions'):
                continue
            for method, operation in methods.items():
                declared = '429' in operation['responses']
                assert declared == (path == messages and method == 'post'), f'{method} {path}'
        schema = spec['paths'][messages]['post']['responses']['429']
        assert schema['content']['application/json']['schema']['$ref'].endswith('/ErrorResponse')


class TestFolderContract:
    """API Contract v1.1 section 12."""

    def test_folders_is_the_only_addition(self, spec):
        """v1.1 is additive: v1's ten routes are untouched."""
        v1_routes = EXPECTED_ROUTES - {("GET", f"{API_PREFIX}/folders")}
        actual = {
            (method.upper(), path)
            for path, methods in spec["paths"].items()
            for method in methods
        }
        assert v1_routes <= actual
        assert len(actual) == 11

    def test_a_folder_carries_a_canonical_path_and_a_display_name(self, spec):
        """Separate fields, because for a legacy folder they differ entirely.

        A client that rebuilt a path by joining names would produce something
        matching no document.
        """
        folder = spec["components"]["schemas"]["FolderOut"]["properties"]
        assert set(folder) == {"path", "name", "parent_path", "depth", "document_count"}

    def test_folders_exposes_no_absolute_path_field(self, spec):
        folder = spec["components"]["schemas"]["FolderOut"]["properties"]
        for banned in ("absolute_path", "shared_root", "source_path", "root"):
            assert banned not in folder

    def test_folders_is_not_paginated(self, spec):
        """A truncated tree is not navigable."""
        response = spec["components"]["schemas"]["FolderListResponse"]["properties"]
        assert set(response) == {"items"}

    def test_search_accepts_folder_path(self, spec):
        params = {
            p["name"] for p in spec["paths"][f"{API_PREFIX}/search"]["get"]["parameters"]
        }
        assert "folder_path" in params

    def test_folders_takes_no_query_parameters(self, spec):
        """The tree is navigation and must not vary with search filters."""
        operation = spec["paths"][f"{API_PREFIX}/folders"]["get"]
        assert not operation.get("parameters")
