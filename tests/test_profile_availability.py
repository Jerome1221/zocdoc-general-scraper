import pytest

from zocdoc_ortho.profile_availability import profile_unavailable_reason


@pytest.mark.parametrize("html", [
    "<body>Not Found. Redirecting to /</body>",
    "<body>Not Found.<br>Redirecting to / </body>",
])
def test_not_found_homepage_redirect(html):
    assert profile_unavailable_reason(html)


def test_homepage_and_not_found_alone_are_not_confirmed_unavailable():
    assert profile_unavailable_reason("<body>Welcome to Zocdoc</body>", final_url="https://www.zocdoc.com/") is None
    assert profile_unavailable_reason("<body>Not Found</body>") is None
    assert profile_unavailable_reason("", final_url="https://www.zocdoc.com/404")
    assert profile_unavailable_reason("", final_url="https://example.com/404") is None


def test_profile_unavailable_reason_detects_zocdoc_404_redirect():
    assert (
        profile_unavailable_reason("<html><body>Not Found. Redirecting to /404</body></html>")
        == "Zocdoc profile returned /404"
    )


def test_profile_unavailable_reason_does_not_match_normal_profile():
    assert (
        profile_unavailable_reason(
            '<html><body><h1 data-test="provider-name">Available Doctor</h1></body></html>'
        )
        is None
    )
