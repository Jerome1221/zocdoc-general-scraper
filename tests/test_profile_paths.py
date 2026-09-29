from zocdoc_ortho.listing import extract_listing_occurrences, parse_page
from zocdoc_ortho.urls import is_profile_url


def test_provider_profile_url_recognizes_doctor_and_specialty_paths():
    assert is_profile_url("https://www.zocdoc.com/doctor/eric-hahn-dc-701889")
    assert is_profile_url("https://www.zocdoc.com/dentist/aaron-wildung-dds-409823")
    assert is_profile_url("https://www.zocdoc.com/dentist/amy-hartsfield-dmd")
    assert is_profile_url("https://www.zocdoc.com/doctor/1114140902-timothy-nettles-dmd")
    assert not is_profile_url("https://www.zocdoc.com/dentists/ahwatukee-foothills-phoenix-az-220850pm")
    assert not is_profile_url("https://www.zocdoc.com/dentists/ahwatukee-foothills-phoenix-az-220850pm/3")
    assert not is_profile_url("https://www.zocdoc.com/about/contactus")
    assert not is_profile_url("https://www.zocdoc.com/patient-help/en")
    assert not is_profile_url("https://www.zocdoc.com/specialty")


def test_dentist_listing_links_are_traced():
    html = """
    <html><body>
      <div data-test="selection-criteria-header">315 verified dentists in Test City</div>
      <div data-test="search-result-card">
        <article data-test="search-result-item">
          <a data-test="doctor-card-info-name" href="https://www.zocdoc.com/dentist/aaron-wildung-dds-409823">
            <span data-test="doctor-card-info-name-prefix">Dr. </span>
            <span data-test="doctor-card-info-name-full">Aaron Wildung</span>
            <span data-test="doctor-card-info-name-suffix">, DDS</span>
          </a>
          <div data-test="doctor-card-info-specialty">Dentist</div>
          <div data-test="doctor-card-info-location">
            <span itemprop="streetAddress">2401 W Glendale Ave</span>
            <span itemprop="addressLocality">Phoenix</span>
            <span itemprop="addressRegion">AZ</span>
            <span itemprop="postalCode">85021</span>
          </div>
        </article>
      </div>
    </body></html>
    """
    page_url = "https://www.zocdoc.com/dentists/test-city-123pm/3"
    parsed = parse_page(html, page_url)
    assert len(parsed["doctors"]) == 1
    assert parsed["doctors"][0]["doctor_url"].endswith("/dentist/aaron-wildung-dds-409823")

    rows = extract_listing_occurrences(
        page_url,
        html,
        {
            "state_group": "Arizona",
            "location": "Test City",
            "page_no": 3,
            "specialty_url": "https://www.zocdoc.com/dentists",
            "specialty_name": "Dentists",
            "specialty_slug": "dentists",
        },
        "dentist-page3.html",
    )
    assert len(rows) == 1
    assert rows[0]["doctor_url"].endswith("/dentist/aaron-wildung-dds-409823")
    assert rows[0]["doctor_name"] == "Dr. Aaron Wildung, DDS"
    assert rows[0]["specialty_name"] == "Dentist"
    assert rows[0]["postal_code"] == "85021"


def test_dentist_listing_links_without_trailing_ids_and_npi_style_doctor_paths_are_traced():
    html = """
    <html><body>
      <div data-test="selection-criteria-header">2 verified dentists in Test City</div>
      <article data-test="search-result-item">
        <a data-test="doctor-card-info-name" href="https://www.zocdoc.com/dentist/amy-hartsfield-dmd">
          <span data-test="doctor-card-info-name-full">Amy Hartsfield</span>
        </a>
      </article>
      <article data-test="search-result-item">
        <a data-test="doctor-card-info-name" href="https://www.zocdoc.com/doctor/1114140902-timothy-nettles-dmd">
          <span data-test="doctor-card-info-name-full">Timothy Nettles</span>
        </a>
      </article>
    </body></html>
    """
    page_url = "https://www.zocdoc.com/dentists/test-city-123pm"
    parsed = parse_page(html, page_url)
    assert len(parsed["doctors"]) == 2
    rows = extract_listing_occurrences(
        page_url, html,
        {
            "state_group": "Alabama",
            "location": "Test City",
            "page_no": 1,
            "specialty_url": "https://www.zocdoc.com/dentists",
            "specialty_name": "Dentists",
            "specialty_slug": "dentists",
        },
        "dentists-test.html",
    )
    assert {r["doctor_url"] for r in rows} == {
        "https://www.zocdoc.com/dentist/amy-hartsfield-dmd",
        "https://www.zocdoc.com/doctor/1114140902-timothy-nettles-dmd",
    }
