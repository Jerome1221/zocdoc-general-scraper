import json

from zocdoc_ortho.listing import extract_listing_occurrences
from zocdoc_ortho.redux import extract_redux_state


def _redux_script(state):
    return f'<script>window.__REDUX_STATE__ = JSON.parse({json.dumps(json.dumps(state))});</script>'


def test_listing_occurrence_preserves_structured_provider_fields():
    profile_url = "https://www.zocdoc.com/doctor/example-doctor-md-123"
    provider = {
        "id": "pr_123",
        "monolithId": 123,
        "npi": "1234567890",
        "profileUrl": profile_url,
        "averageRating": 4.9,
        "reviewCount": 42,
        "acceptsNewPatients": True,
        "offersTelemedicine": True,
        "hasVirtualLocations": True,
        "canHaveAppointments": True,
        "hasNewPatientAvailability": False,
    }
    location = {"id": "lo_456", "zipCode": "30101"}
    state = {
        "listing": {
            "providerLocations": [
                {"id": "pr_123|lo_456", "provider": provider, "location": location}
            ]
        }
    }
    html = f"""
    <html><body>
    {_redux_script(state)}
    <article data-test="search-result-item">
      <a data-test="doctor-card-info-name" href="/doctor/example-doctor-md-123">
        <span data-test="doctor-card-info-name-full">Example Doctor</span>
        <span data-test="doctor-card-info-name-suffix">, MD</span>
      </a>
      <div data-test="doctor-card-info-specialty">Orthopedic Surgeon</div>
      <div data-test="doctor-card-info-location">
        <span itemprop="streetAddress">1 Main St</span>
        <span itemprop="addressLocality">Acworth</span>
        <span itemprop="addressRegion">GA</span>
        <span itemprop="postalCode">30101</span>
      </div>
      <span data-test="video-visits-text">Also offers video visits</span>
      <span data-test="accept-new-patient">Accepting new patients</span>
    </article>
    </body></html>
    """
    assert extract_redux_state(html) is not None
    rows = extract_listing_occurrences(
        "https://www.zocdoc.com/orthopedic-surgeons/acworth-ga-218759pm/2",
        html,
        {"state_group": "Georgia", "location": "Acworth", "page_no": 2},
        "sample.html",
    )
    assert len(rows) == 1
    row = rows[0]
    assert row["provider_id"] == "pr_123"
    assert row["provider_location_key"] == "pr_123|lo_456"
    assert row["postal_code"] == "30101"
    assert row["listing_offers_video_visits"] is True
    assert row["structured_can_have_appointments"] is True


def test_listing_occurrence_recovers_provider_anchor_outside_result_article():
    html = """
    <html><body>
      <article data-test="search-result-item">
        <div>layout wrapper without provider anchor</div>
      </article>
      <div class="provider-name-wrapper">
        <a data-test="doctor-card-info-name" href="/doctor/outside-wrapper-dds-456">
          Outside Wrapper, DDS
        </a>
      </div>
    </body></html>
    """
    rows = extract_listing_occurrences(
        "https://www.zocdoc.com/dentists/test-city-1pm/2",
        html,
        {
            "state_group": "Test State",
            "location": "Test City",
            "page_no": 2,
            "specialty_url": "https://www.zocdoc.com/dentists",
            "specialty_name": "Dentists",
            "specialty_slug": "dentists",
        },
        "page2.html",
    )
    assert len(rows) == 1
    assert rows[0]["doctor_url"].endswith("/doctor/outside-wrapper-dds-456")
    assert rows[0]["listing_page_no"] == 2
