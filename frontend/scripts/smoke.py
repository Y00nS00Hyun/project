"""Read-only HTTP smoke against an actual development API, optionally via Vite."""
import json
import os
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

base = os.environ.get('SMOKE_API_BASE_URL', 'http://localhost:5173/api/v1').rstrip('/')
user_id = os.environ.get('VITE_DEBUG_USER_ID')
if not user_id:
    raise SystemExit('Set VITE_DEBUG_USER_ID to an active development users.id.')
headers = {'X-Debug-User-Id': user_id}


def get(path, authenticated=True):
    return urlopen(Request(base + path, headers=headers if authenticated else {}), timeout=120)


def payload(path):
    with get(path) as response:
        return json.load(response)


def expect_error(path, status, authenticated=True):
    try:
        get(path, authenticated)
    except HTTPError as error:
        assert error.code == status, (path, error.code)
        body = json.load(error)['error']
        assert body['code'] and body['message'] and body['request_id']
    else:
        raise AssertionError(f'Expected {status}: {path}')


browse = payload('/search')
assert browse['page'] == 1 and browse['size'] == 20
assert browse['items'], 'Seed at least one accessible READY document before running smoke.'
assert all(item['snippet'] is None and item['matched_chunk'] is None for item in browse['items'])
query = os.environ.get('SMOKE_QUERY', browse['items'][0]['title'])
search = payload('/search?' + urlencode({'q': query, 'mode': 'default'}))
assert search['items'], 'Use a query matching an accessible downloadable document.'
item = search['items'][0]
assert item['snippet'] and item['matched_chunk']
assert item['matched_chunk']['anchor']['type'] in ('paragraph', 'page', 'none')
path = '/documents/' + item['document_id']
detail = payload(path)
assert detail['document_id'] == item['document_id']
assert detail['current_revision']['revision_id'] == item['current_revision']['revision_id']
revisions = payload(path + '/revisions')
assert revisions['total'] >= 1 and revisions['items']
assert payload('/tags')['items'] is not None
assert payload('/departments')['items'] is not None
with get(path + '/download') as response:
    assert response.read(), 'Downloaded file was empty.'
    assert 'attachment' in response.headers['Content-Disposition']
expect_error('/search', 401, authenticated=False)
expect_error('/search?page=0', 422)
expect_error('/documents/00000000-0000-0000-0000-000000000000', 404)
print('PASS: browse → default search → detail → revisions → download; metadata; 401/404/422')
