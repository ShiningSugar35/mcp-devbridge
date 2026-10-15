"""Privacy-preserving OAuth redirect diagnostics for Gemini account linking."""
from local_dev_mcp_bridge.gateway import _classify_oauth_redirect


def test_consent_redirect_identified_without_exposing_request_id():
    info = _classify_oauth_redirect("/consent?id=sensitive-confirmation")
    assert info == {"oauth_redirect": "consent_page"}


def test_oauth_error_redirect_reports_only_known_error_code():
    info = _classify_oauth_redirect(
        "https://oauth-redirect.googleusercontent.com/continue?"
        "state=top-secret&error=invalid_scope&error_description=private-info"
    )
    assert info == {"oauth_redirect": "oauth_error", "oauth_error": "invalid_scope"}
    assert "top-secret" not in str(info)
    assert "private-info" not in str(info)


def test_external_success_redirect_omits_authorization_code_and_host():
    url = "https://oauth-redirect.googleusercontent.com/end?code=top-secret&state=top-secret"
    info = _classify_oauth_redirect(url)
    assert info == {"oauth_redirect": "client_callback"}
    assert "top-secret" not in str(info)
    assert "googleusercontent" not in str(info)


def test_bad_redirect_is_never_accepted_as_authorized():
    assert _classify_oauth_redirect("nonsense") == {"oauth_redirect": "unrecognized"}
    assert _classify_oauth_redirect("") == {"oauth_redirect": "missing"}
    assert _classify_oauth_redirect("//evil.example/consent?id=xxx") != {"oauth_redirect": "consent_page"}
