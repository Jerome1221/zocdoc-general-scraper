import json

import pandas as pd

from zocdoc_ortho.export import derive_outputs
from zocdoc_ortho.schema import REQUIRED_COLUMNS
from zocdoc_ortho.workspace import Workspace


def _row(location_id, is_virtual):
    row = {column: "" for column in REQUIRED_COLUMNS}
    row.update(
        {
            "state": "PA",
            "full_name": "Alan Taylor, PA",
            "provider_id": "pr_alan",
            "npi": "1100000000",
            "profile_url": "https://www.zocdoc.com/doctor/alan-taylor-pa-1",
            "num_locations": 2,
            "address_line_1": "108 Chancery Pl",
            "city": "Plymouth Meeting",
            "postal_code": "19462",
            "location_id": location_id,
            "is_virtual_location": is_virtual,
            "provider_location_key": f"pr_alan|{location_id}",
            "locations": json.dumps([{"location_id": "lo_physical"}, {"location_id": "lo_virtual"}]),
            "source_zip": "19462",
            "source_zip_state": "PA",
        }
    )
    return row


def test_derived_output_grains(tmp_path):
    workspace = Workspace(tmp_path).ensure()
    frame = pd.DataFrame([_row("lo_physical", False), _row("lo_virtual", True)], columns=REQUIRED_COLUMNS)
    result = derive_outputs(workspace, frame)
    unique = pd.read_csv(result["unique_doctors"], dtype=str, keep_default_na=False)
    virtual = pd.read_csv(result["virtual_locations"], dtype=str, keep_default_na=False)
    physical = pd.read_csv(result["physical_locations"], dtype=str, keep_default_na=False)
    assert len(unique) == 1
    assert "locations" in unique.columns
    assert "address_line_1" not in unique.columns
    assert len(virtual) == 1
    assert len(physical) == 1
