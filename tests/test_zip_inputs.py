import pandas as pd

from zocdoc_ortho.workspace import Workspace
from zocdoc_ortho.zip_inputs import (
    ZIP_SHEET_NAME,
    build_scraped_zip_inputs,
    normalize_state,
    normalize_zip,
)


def test_zip_normalization():
    assert normalize_zip("85018") == "85018"
    assert normalize_zip("601") == "00601"
    assert normalize_zip("85018.0") == "85018"
    assert normalize_zip("85018-1234") == "85018"
    assert normalize_zip("00000") is None
    assert normalize_zip("") is None
    assert normalize_state("pa") == "PA"
    assert normalize_state("Pennsylvania") is None


def test_build_scraped_zip_inputs(tmp_path):
    workspace = Workspace(tmp_path).ensure()
    frame = pd.DataFrame(
        [
            {"state": "PA", "postal_code": "19462"},
            {"state": "PA", "postal_code": "19462"},
            {"state": "NY", "postal_code": "10001"},
            {"state": "PR", "postal_code": "601"},
            {"state": "", "postal_code": "99999"},
        ]
    )

    result = build_scraped_zip_inputs(workspace, final_df=frame)

    long_df = pd.read_csv(result["zip_long_csv"], dtype=str, keep_default_na=False)
    assert list(long_df.columns) == ["state", "zip"]
    assert len(long_df) == 3
    assert set(map(tuple, long_df[["state", "zip"]].to_records(index=False))) == {
        ("NY", "10001"),
        ("PA", "19462"),
        ("PR", "00601"),
    }

    workbook_df = pd.read_excel(
        result["zip_workbook"],
        sheet_name=ZIP_SHEET_NAME,
        dtype=str,
        keep_default_na=False,
    )
    assert list(workbook_df.columns) == ["NY", "PA", "PR"]
    assert workbook_df.loc[0, "NY"] == "10001"
    assert workbook_df.loc[0, "PA"] == "19462"
    assert workbook_df.loc[0, "PR"] == "00601"
    assert result["zip_rows"] == 3
    assert result["zip_states"] == 3
    assert result["zip_conflicts"] == 0
