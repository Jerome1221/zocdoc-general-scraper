import json
import tempfile
import unittest
from email import policy
from email.message import EmailMessage
from pathlib import Path

from zocdoc_ortho.delivery import (
    REQUIRED_COLUMNS,
    UNIQUE_COLUMNS,
    expand_profiles,
    latest_alias_resolutions,
    location_key,
    reviewed_resolutions,
    write_pair,
)


class SubmissionTests(unittest.TestCase):
    def test_migrated_locations_expand(self):
        payload = json.dumps([{"full_name": "Test Doctor", "provider_id": "p1",
            "hospital_affiliations": [], "locations": [
                {"location_id": "1", "postal_code": "01234", "state": "MA", "is_virtual_location": False},
                {"location_id": "2", "state": "NY", "is_virtual_location": True}]}])
        rows = expand_profiles(payload, "https://example.test/doctor")
        self.assertEqual(len(rows), 2)
        self.assertEqual(list(rows[0]), REQUIRED_COLUMNS)
        self.assertEqual(rows[0]["source_zip"], "01234")
        self.assertEqual(len(UNIQUE_COLUMNS), 21)
        self.assertNotEqual(location_key(rows[0]), location_key(rows[1]))

    def test_flattened_rows_not_multiplied(self):
        rows = expand_profiles(json.dumps([{"full_name": "Test", "location_id": "1",
            "locations": '[{"location_id":"1"},{"location_id":"2"}]',
            "hospital_affiliations": "[]", "is_virtual_location": "False"}]), "url")
        self.assertEqual(len(rows), 1)
        self.assertIs(rows[0]["is_virtual_location"], False)

    def test_invalid_and_missing_rows(self):
        for payload in ("[]", "{}", '[{"full_name":"Test"}]', '[{"full_name":""}]'):
            with self.assertRaises(ValueError):
                expand_profiles(payload, "url")

    def test_csv_json_unicode_null_zip(self):
        with tempfile.TemporaryDirectory() as directory:
            rows = [{"zip": "01234", "name": "Doctor, A", "array": [{"text": "a\nb"}], "flag": None}]
            write_pair(Path(directory), "test", list(rows[0]), rows)
            self.assertEqual(json.loads((Path(directory) / "test.json").read_text()), rows)

    def test_shared_provider_location_key_does_not_merge_offices(self):
        first = {"provider_id": "p1", "provider_location_key": "shared", "location_id": "1", "address_line_1": "First"}
        second = dict(first, location_id="2", address_line_1="Second")
        self.assertNotEqual(location_key(first), location_key(second))

    def test_reviewed_resolutions(self):
        class Result:
            def __init__(self, rows):
                self.rows = rows
            def fetchall(self):
                return self.rows
        class Connection:
            def execute(self, query, parameters):
                if 'SELECT DISTINCT' in query:
                    return Result([{'provider_id': 'mark-id', 'npi': '1922291939'}])
                profile = {'full_name': 'David Epstein', 'npi': '1598262511',
                           'location_id': 'loc', 'locations': [], 'review_count': 0}
                return Result([
                    {'doctor_url': parameters[0], 'profile_data_json': json.dumps([dict(profile, can_have_appointments=True)]),
                     'saved_at': '2026-10-06T02:23:37+00:00'},
                    {'doctor_url': parameters[1], 'profile_data_json': json.dumps([profile]),
                     'saved_at': '2026-10-05T13:31:07+00:00'}])
        message = EmailMessage(policy=policy.default.clone(max_line_length=1000))
        message['Snapshot-Content-Location'] = 'https://www.zocdoc.com/doctor/mark-sonnenschine-do-860475'
        message.set_content('Dr. Mark Sonnenschine, DO Oncologist 1922291939 '
                            'Adore Hematology and Oncology PLLC 6201 15th Ave 11219 '
                            'first to leave a review', subtype='html')
        message['Content-Location'] = message['Snapshot-Content-Location']
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'test.mhtml'
            path.write_bytes(message.as_bytes())
            result = reviewed_resolutions(Connection(), path)
        john = result['https://www.zocdoc.com/doctor/john-heiss-md-bethesda']
        self.assertEqual(john['status'], 'unavailable')
        david = result['https://www.zocdoc.com/doctor/david-epstein-md-888823']
        self.assertIs(david['rows'][0]['can_have_appointments'], True)
        mark = result['https://www.zocdoc.com/doctor/mark-sonnenschine-do']['rows'][0]
        self.assertEqual(mark['npi'], '1922291939')
        self.assertIsNone(mark['offers_telemedicine'])
        self.assertEqual(mark['address_line_2'], '#306')

    def test_latest_alias_requires_matching_identity_and_offices(self):
        from types import SimpleNamespace
        profile = {'full_name': 'Doctor', 'provider_id': 'p1', 'npi': '1234567890',
                   'location_id': 'office1', 'locations': [], 'can_have_appointments': False}
        versions = [
            {'doctor_url': 'old', 'profile_data_json': json.dumps([profile]), 'status': 'saved',
             'saved_at': '2026-10-16T12:24:00+00:00'},
            {'doctor_url': 'new', 'profile_data_json': json.dumps([dict(profile, can_have_appointments=True)]),
             'status': 'saved', 'saved_at': '2026-10-16T12:56:00+00:00'}]
        conn = SimpleNamespace(execute=lambda *args: SimpleNamespace(fetchall=lambda: versions))
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'review.json'
            path.write_text(json.dumps([{'provider_id': 'p1', 'versions': [{'doctor_url': 'old'}, {'doctor_url': 'new'}]}]))
            result = latest_alias_resolutions(conn, path)
            self.assertEqual(result['old']['source'], 'new')
            self.assertIs(result['old']['rows'][0]['can_have_appointments'], True)
            versions[0]['profile_data_json'] = json.dumps([dict(profile, npi='9999999999')])
            with self.assertRaises(ValueError):
                latest_alias_resolutions(conn, path)
            versions[0]['profile_data_json'] = json.dumps([dict(profile, location_id='different')])
            with self.assertRaises(ValueError):
                latest_alias_resolutions(conn, path)


if __name__ == "__main__":
    unittest.main()
