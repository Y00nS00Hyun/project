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

#: v1.3. Authentication and account administration. Kept as its own set so the
#: "v1.2 is unchanged" assertions below stay meaningful.
AUTH_ROUTES = {
    ("GET", f"{API_PREFIX}/auth/capability"),
    ("POST", f"{API_PREFIX}/auth/signup"),
    ("POST", f"{API_PREFIX}/auth/login"),
    ("POST", f"{API_PREFIX}/auth/logout"),
    ("GET", f"{API_PREFIX}/auth/me"),
    ("POST", f"{API_PREFIX}/auth/password"),
    ("POST", f"{API_PREFIX}/auth/password/reset"),
    ("GET", f"{API_PREFIX}/admin/users"),
    ("POST", f"{API_PREFIX}/admin/users/{{user_id}}/approve"),
    ("POST", f"{API_PREFIX}/admin/users/{{user_id}}/disable"),
    ("POST", f"{API_PREFIX}/admin/users/{{user_id}}/admin"),
}

#: Read-only additions after v1.3. The extracted-text preview is its own
#: route rather than a field on the detail response so a long document is
#: paged instead of shipped whole.
PREVIEW_ROUTES = {
    ("GET", f"{API_PREFIX}/documents/{{document_id}}/text"),
}

ALL_ROUTES = EXPECTED_ROUTES | AUTH_ROUTES | PREVIEW_ROUTES

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
        assert actual == ALL_ROUTES

    def test_no_write_endpoint_touches_a_document(self, spec):
        """Nothing that was added may write to a document or its permissions.

        The POST allow-list is explicit rather than a prefix rule: chat
        persists turns, auth manages sessions and accounts, and administration
        approves people. None of them is a route by which a document, a
        revision or a permission row can be created or changed -- the corpus
        stays read-only, and the ACL is still edited outside the API.
        """
        allowed_post_prefixes = (
            f'{API_PREFIX}/chat/sessions',
            f'{API_PREFIX}/auth/',
            f'{API_PREFIX}/admin/users',
        )
        for path, ops in spec["paths"].items():
            for method in ops:
                assert method.upper() not in {"PUT", "PATCH", "DELETE"}, (
                    f"{method.upper()} {path} must not exist"
                )
                if method.upper() == 'POST':
                    assert path.startswith(allowed_post_prefixes), path
                    assert '/documents' not in path
                    assert 'permission' not in path

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
            # v1.2: the one addition to v1's set. Needed because a feature that
            # is configured off is not a server fault, and reporting it as one
            # leaves a client with nothing better to do than retry forever.
            "FEATURE_UNAVAILABLE",
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
        # v1's eight, plus folder_path from the v1.1 additive revision, plus
        # top_level_only -- the complement of folder_path, which no value of
        # folder_path can express because every path starts at the root.
        assert params == {"q", "mode", "page", "size", "department_id", "year",
                          "tag_id", "file_type", "folder_path", "top_level_only"}

    def test_size_bounds_are_declared(self, spec):
        for p in spec["paths"][f"{API_PREFIX}/search"]["get"]["parameters"]:
            if p["name"] == "size":
                schema = p["schema"]
                assert schema.get("minimum") == 1 and schema.get("maximum") == 100
                return
        pytest.fail("size parameter not declared")


class TestDocumentDateContract:
    """Three dates, and each one says which it is.

    They answer different questions and were previously reduced to two, one of
    which was mislabelled: `updated_at` is when this system last touched the
    row and was shown as 수정일.
    """

    def test_detail_carries_all_three(self, spec):
        fields = set(response_schemas(spec)["DocumentDetailOut"]["properties"])
        assert {"created_at", "updated_at", "source_modified_at", "document_date"} <= fields

    def test_detail_reports_what_a_download_would_fetch(self, spec):
        # The current revision's size, like every other figure in the detail
        # response -- not the newest file on disk.
        fields = set(response_schemas(spec)["DocumentDetailOut"]["properties"])
        assert "file_size" in fields

    def test_the_document_date_is_a_date_not_a_timestamp(self, spec):
        # A cover states a day. Serialising it as an instant would invent a
        # time and a timezone, and the invented timezone shifts the day.
        schema = response_schemas(spec)["DocumentDetailOut"]["properties"]["document_date"]
        assert "date" in str(schema)
        assert "date-time" not in str(schema)

    def test_the_document_date_is_nullable(self, spec):
        # Most documents have none, and that is the correct answer for them.
        schema = response_schemas(spec)["DocumentDetailOut"]["properties"]["document_date"]
        assert "null" in str(schema)


class TestDocumentSummaryContract:
    def test_detail_carries_the_precomputed_summary(self, spec):
        schemas = response_schemas(spec)
        assert 'summary' in schemas['DocumentDetailOut']['properties']
        assert 'chat' in schemas['DocumentDetailOut']['properties']
        assert set(schemas['ChatCapabilityOut']['properties']) == {'available'}
        assert set(schemas['SummaryOut']['properties']) == {
            'state', 'reason', 'content', 'generated_at', 'available', 'revision_id',
        }

    def test_summary_does_not_expose_the_provider_or_the_model(self, spec):
        schemas = response_schemas(spec)
        # v1.2 decision 2: which service wrote a summary is an operational fact
        # about our pipeline, not something a reader should weigh an answer by.
        assert not {'provider', 'model', 'summary_provider', 'summary_model',
                    'prompt_version', 'summary_prompt_version'} & set(
                        schemas['SummaryOut']['properties'])

    def test_availability_is_a_capability_field_not_a_summary_state(self, spec):
        schemas = response_schemas(spec)
        # Supplement A: "generation is off in this deployment" is answered here,
        # so it never needs a new value in the database status CHECK.
        assert schemas['SummaryOut']['properties']['available']['type'] == 'boolean'
        assert schemas['SummaryOut']['properties']['state']['type'] == 'string'


class TestAuthContract:
    """v1.3. The authentication surface mentions no department anywhere.

    The organisation does not use them. The column and the department ACL
    principal stay for the documents that carry them, but nothing a person
    sees or sends in order to sign in refers to one.
    """

    def test_no_auth_schema_carries_a_department(self, spec):
        schemas = response_schemas(spec)
        for name in (
            'SignupRequest', 'LoginRequest', 'MeResponse', 'AdminUserOut',
            'ChangePasswordRequest', 'ResetPasswordRequest', 'SetAdminRequest',
            'SignupResponse', 'AuthCapabilityResponse',
        ):
            fields = set(schemas[name]['properties'])
            assert not {'department', 'department_id', 'department_name'} & fields, name

    def test_signup_takes_four_fields(self, spec):
        schemas = response_schemas(spec)
        assert set(schemas['SignupRequest']['properties']) == {
            'login_id', 'name', 'password', 'password_confirm',
        }
        assert schemas['SignupRequest']['additionalProperties'] is False

    def test_no_auth_response_can_return_a_credential(self, spec):
        schemas = response_schemas(spec)
        for name in ('MeResponse', 'AdminUserOut', 'SignupResponse'):
            fields = set(schemas[name]['properties'])
            assert not {
                'password', 'password_hash', 'token', 'token_hash',
                'session_token', 'reset_token', 'reset_token_hash',
            } & fields, name

    def test_approval_has_no_request_body(self, spec):
        approve = spec['paths'][f'{API_PREFIX}/admin/users/{{user_id}}/approve']['post']
        # One decision -- may this person sign in. A body would be somewhere
        # for a second one to creep in.
        assert 'requestBody' not in approve

    def test_there_is_no_department_endpoint_for_administration(self, spec):
        assert f'{API_PREFIX}/admin/departments' not in spec['paths']
        # The pre-existing metadata endpoint is untouched: documents still use
        # departments, and removing it would be an unrelated breaking change.
        assert f'{API_PREFIX}/departments' in spec['paths']


class TestDisabledGenerationContract:
    def test_a_disabled_feature_is_not_a_server_error(self, spec):
        from api.errors import ERROR_CODES

        # 500 tells a client to retry something that will never start working,
        # and gives a UI nothing to disable a control with.
        assert ERROR_CODES['FEATURE_UNAVAILABLE'] == 503

    def test_only_the_message_route_declares_it(self, spec):
        paths = spec['paths']
        messages = f'{API_PREFIX}/chat/sessions/{{session_id}}/messages'
        assert '503' in paths[messages]['post']['responses']
        # The session routes generate nothing, so they can never be refused for
        # want of a provider.
        assert '503' not in paths[f'{API_PREFIX}/chat/sessions']['post']['responses']
        assert '503' not in paths[f'{API_PREFIX}/search']['get']['responses']


class TestChatContract:
    def test_request_shapes_forbid_client_identity(self, spec):
        schemas = response_schemas(spec)
        # v1.2 adds CreateSessionRequest.document_id. SendMessageRequest is
        # deliberately unchanged: the scope is fixed when the session is
        # created, so a per-message document_id could never widen it.
        for name, fields in [
            ('CreateSessionRequest', {'title', 'document_id'}),
            ('SendMessageRequest', {'message'}),
        ]:
            assert set(schemas[name]['properties']) == fields
            assert schemas[name]['additionalProperties'] is False
        assert 'required' not in schemas['CreateSessionRequest']
        message = schemas['SendMessageRequest']['properties']['message']
        assert message['minLength'] == 1 and message['maxLength'] == 4000

    def test_no_request_body_carries_an_identity(self, spec):
        schemas = response_schemas(spec)
        for name in ('CreateSessionRequest', 'SendMessageRequest'):
            assert not {'user_id', 'department_id', 'user'} & set(schemas[name]['properties'])

    def test_response_shapes_match_contract(self, spec):
        schemas = response_schemas(spec)
        session = {'session_id', 'title', 'created_at', 'updated_at', 'document_scope'}
        assert set(schemas['SessionOut']['properties']) == session
        assert set(schemas['SessionDetail']['properties']) == session | {'messages'}
        assert set(schemas['SessionListItem']['properties']) == session | {'message_count'}
        assert set(schemas['SendMessageResponse']['properties']) == {
            'message_id', 'answer', 'refused', 'sources', 'created_at',
        }
        assert schemas['SendMessageResponse']['properties']['refused']['type'] == 'boolean'
        assert schemas['AssistantMessage']['properties']['refused']['type'] == 'boolean'
        assert schemas['AssistantMessage']['properties']['content_hidden']['type'] == 'boolean'

    def test_scope_omits_the_title_when_the_document_is_not_readable(self, spec):
        schemas = response_schemas(spec)
        # Same rule as InaccessibleSource: the title is convenience data about
        # a document, so losing read access must also hide the name.
        assert set(schemas['InaccessibleScope']['properties']) == {'document_id', 'accessible'}
        assert set(schemas['AccessibleScope']['properties']) == {
            'document_id', 'accessible', 'title',
        }
        scope = schemas['SessionOut']['properties']['document_scope']
        assert 'provider' not in str(scope) and 'model' not in str(scope)

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
        """Every revision so far has been additive: earlier routes are untouched."""
        v1_routes = EXPECTED_ROUTES - {("GET", f"{API_PREFIX}/folders")}
        actual = {
            (method.upper(), path)
            for path, methods in spec["paths"].items()
            for method in methods
        }
        assert v1_routes <= actual
        # 11 through v1.2, the eleven v1.3 authentication routes, and the
        # extracted-text preview.
        assert len(actual) == len(ALL_ROUTES) == 23

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
        """A truncated tree is not navigable.

        `total_documents` is a corpus size, not a page count: there is still no
        page, size, offset, cursor or total-pages field to page with.
        """
        response = spec["components"]["schemas"]["FolderListResponse"]["properties"]
        assert set(response) == {"items", "total_documents", "top_level_documents"}
        assert not {"page", "size", "offset", "cursor", "next", "total_pages"} & set(response)

    def test_the_root_count_is_not_the_sum_of_the_folder_counts(self, spec):
        """Documented, because summing would be the obvious wrong assumption.

        A document at the top of the shared folder belongs to no folder and
        appears in no item -- which is the entire corpus in this deployment.
        """
        response = spec["components"]["schemas"]["FolderListResponse"]
        assert "total_documents" in response["properties"]
        assert response["properties"]["total_documents"]["type"] == "integer"

    def test_the_counts_partition_the_corpus(self, spec):
        """Folders plus top-level documents account for everything.

        A document at the top of the shared folder contributes no folder row,
        so a client that summed `items` alone would under-report -- which in a
        flat shared folder is nearly the whole corpus.
        """
        response = spec["components"]["schemas"]["FolderListResponse"]["properties"]
        assert response["total_documents"]["type"] == "integer"
        assert response["top_level_documents"]["type"] == "integer"

    def test_search_selects_top_level_documents_with_its_own_flag(self, spec):
        params = {
            p["name"] for p in spec["paths"][f"{API_PREFIX}/search"]["get"]["parameters"]
        }
        # Not a reserved folder_path value: every path starts at the root, so no
        # prefix picks out exactly the documents not under one -- and a reserved
        # string could one day collide with a real folder name.
        assert "top_level_only" in params

    def test_search_accepts_folder_path(self, spec):
        params = {
            p["name"] for p in spec["paths"][f"{API_PREFIX}/search"]["get"]["parameters"]
        }
        assert "folder_path" in params

    def test_folders_takes_no_query_parameters(self, spec):
        """The tree is navigation and must not vary with search filters."""
        operation = spec["paths"][f"{API_PREFIX}/folders"]["get"]
        assert not operation.get("parameters")
