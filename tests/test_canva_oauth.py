from urllib.parse import parse_qs, urlparse

from tyre_agents.canva_oauth import create_pkce_request, exchange_code


def test_pkce_request_contains_canva_oauth_parameters():
    request = create_pkce_request("client-1", "https://app.test/callback")
    query = parse_qs(urlparse(request.authorization_url).query)

    assert query["client_id"] == ["client-1"]
    assert query["redirect_uri"] == ["https://app.test/callback"]
    assert query["code_challenge_method"] == ["S256"]
    assert query["state"] == [request.state]
    assert len(request.code_verifier) >= 43


class FakeResponse:
    status_code = 200

    def json(self):
        return {"access_token": "access-1", "refresh_token": "refresh-1", "expires_in": 14400}


class FakeSession:
    def __init__(self):
        self.calls = []

    def post(self, url, **kwargs):
        self.calls.append((url, kwargs))
        return FakeResponse()


def test_exchange_code_uses_basic_auth_and_pkce_fields():
    session = FakeSession()
    result = exchange_code("code-1", "verifier-1", "client-1", "secret-1", "https://app.test/callback", session=session)

    assert result["access_token"] == "access-1"
    assert session.calls[0][1]["auth"] == ("client-1", "secret-1")
    assert session.calls[0][1]["data"]["code_verifier"] == "verifier-1"
