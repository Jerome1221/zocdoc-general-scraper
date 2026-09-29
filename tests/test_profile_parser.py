import json

from zocdoc_ortho.profile import parse_profile_rows


def _redux_script(state):
    return f'<script>window.__REDUX_STATE__ = JSON.parse({json.dumps(json.dumps(state))});</script>'


def test_profile_parser_uses_direct_and_inferred_fields():
    url = "https://www.zocdoc.com/doctor/example-doctor-md-123"
    provider = {
        "id": "pr_123",
        "monolithId": 123,
        "npi": "1234567890",
        "profileUrl": url,
        "approvedFullName": "Dr. Example Doctor, MD",
        "firstName": "Example",
        "lastName": "Doctor",
        "prenominal": "Dr.",
        "postnominal": "MD",
        "averageRating": 4.8,
        "reviewCount": 50,
        "acceptsNewPatients": True,
        "onlySeesChildren": False,
        "isBookable": True,
        "hasVirtualLocations": True,
        "statement": "Sample bio",
        "relevantSpecialty": {"name": "Orthopedic Surgeon"},
        "practice": {"name": "Example Orthopedics"},
        "approvedLocations": [
            {
                "id": "lo_physical",
                "address1": "1 Main St",
                "city": "Acworth",
                "state": "GA",
                "zipCode": "30101",
                "phone": "555-0100",
                "isVirtual": False,
                "virtualVisitType": "None",
            },
            {
                "id": "lo_virtual",
                "address1": "1 Main St",
                "city": "Acworth",
                "state": "GA",
                "zipCode": "30101",
                "phone": "555-0100",
                "isVirtual": True,
                "virtualVisitType": "ThirdPartyVideoVisit",
            },
        ],
    }
    state = {
        "profile": {"data": {"provider": provider}},
        "providerLocations": [
            {"id": "pr_123|lo_physical", "provider": provider, "location": provider["approvedLocations"][0]},
            {"id": "pr_123|lo_virtual", "provider": provider, "location": provider["approvedLocations"][1]},
        ],
    }
    html = f"<html><head>{_redux_script(state)}</head><body><h1>Dr. Example Doctor, MD</h1></body></html>"
    trace_rows = [
        {
            "doctor_url": url,
            "structured_can_have_appointments": True,
            "listing_offers_video_visits": True,
            "provider_location_key": "pr_123|lo_physical",
            "postal_code": "30101",
        }
    ]
    rows, diag = parse_profile_rows(url, html, trace_rows)
    assert len(rows) == 2
    assert rows[0]["accepts_new_patients"] is True
    assert rows[0]["only_sees_children"] is False
    assert all(row["offers_telemedicine"] is True for row in rows)
    assert all(row["can_have_appointments"] is True for row in rows)
    assert {row["is_virtual_location"] for row in rows} == {True, False}
    assert rows[0]["source_zip"] == "30101"
    assert diag["can_have_appointments_source"] == "listing.canHaveAppointments"


def test_nonbookable_profile_falls_back_to_isbookable():
    url = "https://www.zocdoc.com/doctor/nonbookable-md"
    provider = {
        "id": "pr_no",
        "npi": "9999999999",
        "profileUrl": url,
        "approvedFullName": "Non Bookable, MD",
        "isBookable": False,
        "isPreview": True,
        "isEnabledForMarketplace": False,
        "approvedLocations": [{"id": "lo_no", "city": "Carrollton", "state": "KY", "zipCode": "41008", "isVirtual": False}],
    }
    state = {"profile": {"data": {"provider": provider}}}
    html = f"<html><head>{_redux_script(state)}</head><body></body></html>"
    rows, diag = parse_profile_rows(url, html, [])
    assert rows[0]["can_have_appointments"] is False
    assert diag["can_have_appointments_source"] == "profile.isBookable"
