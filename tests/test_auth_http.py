"""Local authentication over real HTTP, against a real database.

The layer the service tests cannot see: cookie attributes, status codes, which
routes the guard actually covers, and whether an authenticated session really
flows into the existing ACL rather than into a second copy of it.
"""

from __future__ import annotations

import psycopg
import pytest
from fastapi.testclient import TestClient

from api import dependencies
from api.app import create_app
from auth.config import SESSION_COOKIE
from auth.repository import AuthRepository
from auth.service import reset_rate_limits
from ingestion.config import config_from_env
from search.service import SearchService

from support import pgtest
from test_api_http import DeterministicEmbedder
from test_search_backend import Corpus

PASSWORD = 'test-password-0001'


@pytest.fixture(scope='module')
def auth_dsn():
    server = pgtest.start_server()
    assert not pgtest.check_extensions_available(server)
    return pgtest.psycopg_url(pgtest.migrated_database(server, 'auth_http_test'))


@pytest.fixture
def conn(auth_dsn):
    with psycopg.connect(auth_dsn, autocommit=True) as connection:
        pgtest.truncate_all(connection)
        yield connection


@pytest.fixture(autouse=True)
def clean_rate_limits():
    reset_rate_limits()
    yield
    reset_rate_limits()


@pytest.fixture
def world(conn):
    corpus = Corpus(conn, DeterministicEmbedder())
    document, _ = corpus.document('서버 장애 지침', text='서버 장애 대응 절차')
    return {'document': document, 'corpus': corpus}


def build_app(monkeypatch, dsn, tmp_path, *, local_auth=True, signup=True,
              app_env='test'):
    monkeypatch.setenv('APP_ENV', app_env)
    monkeypatch.setenv('SHARED_ROOT', str(tmp_path))
    monkeypatch.setenv('DATABASE_URL', dsn)
    monkeypatch.setenv('LOCAL_AUTH_ENABLED', 'true' if local_auth else 'false')
    monkeypatch.setenv('SELF_SIGNUP_ENABLED', 'true' if signup else 'false')
    monkeypatch.delenv('AUTH_COOKIE_SECURE', raising=False)
    dependencies.get_config.cache_clear()
    dependencies.get_dsn.cache_clear()
    application = create_app()
    application.dependency_overrides[dependencies.get_search_service] = lambda: SearchService(
        dependencies.connection_factory(), config_from_env(), DeterministicEmbedder(),
    )
    return application


@pytest.fixture
def client(auth_dsn, conn, tmp_path, monkeypatch, world):
    application = build_app(monkeypatch, auth_dsn, tmp_path)
    with TestClient(application) as test_client:
        yield test_client
    dependencies.get_config.cache_clear()
    dependencies.get_dsn.cache_clear()


def do_signup(client, login_id='tester', password=PASSWORD):
    return client.post('/api/v1/auth/signup', json={
        'login_id': login_id, 'name': '테스터',
        'password': password, 'password_confirm': password,
    })


def do_login(client, login_id='tester', password=PASSWORD):
    return client.post('/api/v1/auth/login', json={'login_id': login_id, 'password': password})


def approve(conn, connection_factory, login_id):
    """Let an account in. Grants no document permission -- that is separate."""
    repository = AuthRepository(connection_factory)
    credential = repository.credential(login_id)
    repository.set_status(str(credential['user_id']), 'ACTIVE')
    return str(credential['user_id'])


def grant_read(conn, user_id, document_id):
    """An ordinary USER principal READ row -- what the CLI writes."""
    with conn.cursor() as cur:
        cur.execute(
            'INSERT INTO document_permissions (document_id, user_id, permission) '
            "VALUES (%s, %s, 'READ')",
            (document_id, user_id),
        )


@pytest.fixture
def factory(auth_dsn):
    def make():
        return psycopg.connect(auth_dsn)

    return make


# ---------------------------------------------------------------------------
# Capability and signup
# ---------------------------------------------------------------------------

def test_capability_is_readable_without_a_session(client):
    body = client.get('/api/v1/auth/capability').json()
    assert body == {'local_auth_enabled': True, 'signup_enabled': True}


def test_signup_returns_pending_and_no_session(client):
    response = do_signup(client)
    assert response.status_code == 201
    assert response.json()['status'] == 'PENDING'
    # No cookie: the account cannot be used yet, so issuing one would hand out
    # a session that every protected route then rejects.
    assert SESSION_COOKIE not in response.cookies
    assert client.get('/api/v1/auth/me').status_code == 401


@pytest.mark.parametrize('extra', [
    {'department_id': '00000000-0000-0000-0000-000000000001'},
    {'role': 'ADMIN'},
    {'status': 'ACTIVE'},
])
def test_signup_rejects_anything_beyond_the_four_fields(client, extra):
    response = client.post('/api/v1/auth/signup', json={
        'login_id': 'sneaky', 'name': '테스터',
        'password': PASSWORD, 'password_confirm': PASSWORD, **extra,
    })
    # extra='forbid'. There is no shape in which an applicant chooses any of
    # these -- department included, since the organisation does not use them.
    assert response.status_code == 422


def test_signup_rejects_an_admin_field(client):
    response = client.post('/api/v1/auth/signup', json={
        'login_id': 'sneaky', 'name': '테스터',
        'password': PASSWORD, 'password_confirm': PASSWORD,
        'is_system_admin': True,
    })
    assert response.status_code == 422


def test_signup_is_unavailable_when_switched_off(
    auth_dsn, conn, tmp_path, monkeypatch, world,
):
    application = build_app(monkeypatch, auth_dsn, tmp_path, signup=False)
    with TestClient(application) as client:
        assert do_signup(client).status_code == 503
        assert client.get('/api/v1/auth/capability').json()['signup_enabled'] is False


def test_everything_is_unavailable_when_local_auth_is_off(
    auth_dsn, conn, tmp_path, monkeypatch, world,
):
    application = build_app(monkeypatch, auth_dsn, tmp_path, local_auth=False)
    with TestClient(application) as client:
        assert do_signup(client).status_code == 503
        assert do_login(client).status_code == 503


# ---------------------------------------------------------------------------
# Login, cookie, session
# ---------------------------------------------------------------------------

def test_pending_login_is_refused_with_the_approval_message(client):
    do_signup(client)
    response = do_login(client)
    assert response.status_code == 403
    assert response.json()['error']['message'] == '관리자 승인 대기 중입니다.'


def test_wrong_password_and_unknown_account_are_indistinguishable(client, conn, factory, world):
    do_signup(client)
    approve(conn, factory, 'tester')
    wrong = do_login(client, password='test-password-9999')
    unknown = do_login(client, login_id='nobody-at-all', password='test-password-9999')
    assert wrong.status_code == unknown.status_code == 401
    assert wrong.json()['error']['message'] == unknown.json()['error']['message']
    assert wrong.json()['error']['code'] == unknown.json()['error']['code']


def test_the_session_cookie_is_httponly_and_lax(client, conn, factory, world):
    do_signup(client)
    approve(conn, factory, 'tester')
    response = do_login(client)
    assert response.status_code == 200

    header = response.headers['set-cookie']
    assert 'HttpOnly' in header
    assert 'SameSite=lax' in header.lower() or 'samesite=lax' in header.lower()
    # Plain HTTP in this environment: a Secure cookie would never be stored.
    assert 'Secure' not in header
    assert 'Path=/' in header


def test_the_cookie_is_secure_when_configured(
    auth_dsn, conn, tmp_path, monkeypatch, world, factory,
):
    application = build_app(monkeypatch, auth_dsn, tmp_path)
    monkeypatch.setenv('AUTH_COOKIE_SECURE', 'true')
    with TestClient(application) as client:
        do_signup(client)
        approve(conn, factory, 'tester')
        assert 'Secure' in do_login(client).headers['set-cookie']


def test_the_response_never_carries_a_hash_or_a_token(client, conn, factory, world):
    do_signup(client)
    approve(conn, factory, 'tester')
    body = do_login(client).json()
    # No department either: the organisation does not use them, so nothing in
    # the user-facing API mentions one.
    assert set(body) == {'user_id', 'name', 'is_system_admin'}


def test_me_reports_the_signed_in_user(client, conn, factory, world):
    do_signup(client)
    approve(conn, factory, 'tester')
    do_login(client)
    body = client.get('/api/v1/auth/me').json()
    assert body['name'] == '테스터'
    assert body['is_system_admin'] is False
    assert 'department_name' not in body and 'department_id' not in body


def test_the_session_survives_a_new_request(client, conn, factory, world):
    do_signup(client)
    approve(conn, factory, 'tester')
    do_login(client)
    # The cookie jar is the browser; nothing is kept in the page.
    assert client.get('/api/v1/auth/me').status_code == 200
    assert client.get('/api/v1/search?size=5').status_code == 200


def test_logout_ends_the_session_for_everyone_holding_the_token(
    client, conn, factory, world,
):
    do_signup(client)
    approve(conn, factory, 'tester')
    do_login(client)
    token = client.cookies[SESSION_COOKIE]

    assert client.post('/api/v1/auth/logout').status_code == 204
    assert client.get('/api/v1/auth/me').status_code == 401

    # A copy of the token kept elsewhere is dead too: revocation is server-side,
    # not merely a cleared cookie.
    client.cookies.set(SESSION_COOKIE, token)
    assert client.get('/api/v1/auth/me').status_code == 401


def test_a_forged_cookie_is_just_unauthenticated(client):
    client.cookies.set(SESSION_COOKIE, 'not-a-real-token')
    assert client.get('/api/v1/auth/me').status_code == 401
    assert client.get('/api/v1/search').status_code == 401


# ---------------------------------------------------------------------------
# The session flows into the existing ACL, not around it
# ---------------------------------------------------------------------------

def test_an_approved_account_sees_only_what_it_was_granted(client, conn, factory, world):
    do_signup(client)
    user_id = approve(conn, factory, 'tester')
    grant_read(conn, user_id, world['document'])
    do_login(client)

    body = client.get('/api/v1/search?size=10').json()
    assert [item['document_id'] for item in body['items']] == [world['document']]


def test_approval_alone_grants_nothing(client, conn, factory, world):
    """Default deny, stated as a test.

    Approval decides whether somebody may sign in. What they may read is a
    separate decision recorded in document_permissions, and until it is made
    the answer is nothing.
    """
    do_signup(client)
    approve(conn, factory, 'tester')
    do_login(client)
    assert client.get('/api/v1/search?size=10').json()['total'] == 0


def test_a_document_granted_to_somebody_else_stays_invisible(
    client, conn, factory, world,
):
    other, _ = world['corpus'].document('다른 사람 문서', text='서버 장애 기밀')
    stranger = world['corpus'].user('stranger')
    world['corpus'].grant(other, user_id=stranger)

    do_signup(client)
    user_id = approve(conn, factory, 'tester')
    grant_read(conn, user_id, world['document'])
    do_login(client)
    ids = [item['document_id'] for item in client.get('/api/v1/search?size=10').json()['items']]
    assert other not in ids


def test_a_department_grant_is_not_what_gives_access_now(client, conn, factory, world):
    """Local Auth no longer depends on the department principal.

    The principal itself is untouched -- documents that still carry a
    department grant keep working -- but an account created through signup has
    no department, so nothing it can read comes from that route.
    """
    do_signup(client)
    user_id = approve(conn, factory, 'tester')
    grant_read(conn, user_id, world['document'])
    do_login(client)

    with conn.cursor() as cur:
        cur.execute('SELECT count(*) FROM document_permissions WHERE department_id IS NOT NULL')
        assert cur.fetchone()[0] == 0
        cur.execute("SELECT department_id FROM users WHERE sso_subject = 'local:tester'")
        assert cur.fetchone()[0] is None

    assert client.get('/api/v1/search?size=10').json()['total'] == 1


def test_losing_approval_ends_access_mid_session(client, conn, factory, world):
    do_signup(client)
    user_id = approve(conn, factory, 'tester')
    grant_read(conn, user_id, world['document'])
    do_login(client)
    assert client.get('/api/v1/search').status_code == 200

    AuthRepository(factory).set_status(user_id, 'DISABLED')
    # Re-checked on every request rather than trusted from login time.
    assert client.get('/api/v1/search').status_code == 401


# ---------------------------------------------------------------------------
# Administration
# ---------------------------------------------------------------------------

def make_admin(client, conn, factory, world, login_id='admin1'):
    do_signup(client, login_id=login_id)
    user_id = approve(conn, factory, login_id)
    AuthRepository(factory).grant_system_admin(login_id)
    do_login(client, login_id=login_id)
    return user_id


def test_an_ordinary_user_gets_403_from_the_admin_api(client, conn, factory, world):
    do_signup(client)
    approve(conn, factory, 'tester')
    do_login(client)
    for path in ('/api/v1/admin/users',):
        response = client.get(path)
        assert response.status_code == 403
        assert response.json()['error']['code'] == 'FORBIDDEN'


def test_an_anonymous_caller_gets_401_from_the_admin_api(client):
    assert client.get('/api/v1/admin/users').status_code == 401


def test_an_administrator_can_approve_and_the_account_then_works(
    client, conn, factory, world,
):
    do_signup(client, login_id='waiting')
    make_admin(client, conn, factory, world)

    pending = client.get('/api/v1/admin/users?status=PENDING').json()
    target = next(row for row in pending if row['login_id'] == 'waiting')
    # No body: approval is one decision, and a body would be somewhere for a
    # second one to creep in.
    approved = client.post(f"/api/v1/admin/users/{target['user_id']}/approve")
    assert approved.status_code == 200
    assert approved.json()['status'] == 'ACTIVE'
    assert approved.json()['is_system_admin'] is False

    client.post('/api/v1/auth/logout')
    assert do_login(client, login_id='waiting').status_code == 200


def test_the_admin_list_never_exposes_a_hash(client, conn, factory, world):
    make_admin(client, conn, factory, world)
    body = client.get('/api/v1/admin/users').text
    assert 'argon2' not in body and 'password' not in body and 'token' not in body


def test_an_administrator_cannot_disable_themselves(client, conn, factory, world):
    admin_id = make_admin(client, conn, factory, world)
    response = client.post(f'/api/v1/admin/users/{admin_id}/disable')
    assert response.status_code == 422


def test_disabling_logs_the_other_account_out(client, conn, factory, world):
    do_signup(client)
    victim = approve(conn, factory, 'tester')
    make_admin(client, conn, factory, world)
    assert client.post(f'/api/v1/admin/users/{victim}/disable').status_code == 200

    client.post('/api/v1/auth/logout')
    assert do_login(client).status_code == 403


# ---------------------------------------------------------------------------
# Production
# ---------------------------------------------------------------------------

def test_the_debug_header_is_refused_in_production(
    auth_dsn, conn, tmp_path, monkeypatch, world, factory,
):
    application = build_app(monkeypatch, auth_dsn, tmp_path, app_env='production')
    with TestClient(application) as client:
        do_signup(client)
        user_id = approve(conn, factory, 'tester')
        response = client.get('/api/v1/search', headers={'X-Debug-User-Id': user_id})
        assert response.status_code == 401


def test_a_real_session_still_works_in_production(
    auth_dsn, conn, tmp_path, monkeypatch, world, factory,
):
    """Local auth is the operational login, so production must accept it.

    There is no SSO to fall back to; refusing here would leave a production
    deployment with no way for anyone to log in at all.
    """
    application = build_app(monkeypatch, auth_dsn, tmp_path, app_env='production')
    with TestClient(application) as client:
        do_signup(client)
        approve(conn, factory, 'tester')
        assert do_login(client).status_code == 200
        assert client.get('/api/v1/search').status_code == 200


def test_production_with_local_auth_off_admits_nobody(
    auth_dsn, conn, tmp_path, monkeypatch, world, factory,
):
    application = build_app(monkeypatch, auth_dsn, tmp_path, app_env='production', local_auth=False)
    with TestClient(application) as client:
        assert do_login(client).status_code == 503
        assert client.get('/api/v1/search').status_code == 401
