from zocdoc_ortho.urls import base_location_url, normalize_url, page_number, safe_slug


def test_url_normalization_and_pagination():
    url = "https://www.zocdoc.com/orthopedic-surgeons/acworth-ga-218759pm/2?foo=bar#x"
    assert normalize_url(url) == "https://www.zocdoc.com/orthopedic-surgeons/acworth-ga-218759pm/2"
    assert base_location_url(url) == "https://www.zocdoc.com/orthopedic-surgeons/acworth-ga-218759pm"
    assert page_number(url) == 2
    assert safe_slug(url).endswith(".html")
