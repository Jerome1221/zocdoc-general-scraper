from __future__ import annotations

import re
from pathlib import Path

import pandas as pd

from .workspace import Workspace

ZIP_WORKBOOK_FILENAME = "zocdoc_scraped_zipcodes_by_state.xlsx"
ZIP_LONG_FILENAME = "zocdoc_scraped_zipcodes_long.csv"
ZIP_CONFLICTS_FILENAME = "zocdoc_scraped_zipcode_conflicts.csv"
ZIP_SHEET_NAME = "UC Extended Zipcodes"
ZIP_COUNTS_SHEET_NAME = "ZIP Counts"


def normalize_state(value) -> str | None:
    """Normalize a two-letter state/area code, otherwise return None."""
    text = _clean(value).upper()
    return text if re.fullmatch(r"[A-Z]{2}", text) else None


def normalize_zip(value) -> str | None:
    """Normalize common ZIP representations to a five-digit ZIP string."""
    text = _clean(value)
    if not text:
        return None

    if re.fullmatch(r"\d{1,5}\.0", text):
        text = text[:-2]

    if re.fullmatch(r"\d{1,5}", text):
        normalized = text.zfill(5)
        return None if normalized == "00000" else normalized

    match = re.search(r"(?<!\d)(\d{5})(?:-\d{4})?(?!\d)", text)
    if match:
        normalized = match.group(1)
        return None if normalized == "00000" else normalized

    return None


def build_scraped_zip_inputs(
    workspace: Workspace,
    final_df: pd.DataFrame | None = None,
    *,
    input_path: Path | None = None,
) -> dict[str, Path | int]:
    """Build original-scraper-compatible ZIP inputs from collected provider locations.

    The source grain is the canonical provider-location output. ZIP/state pairs are
    deduplicated, so repeated providers at the same ZIP do not create duplicate targets.
    """
    workspace.ensure()

    if final_df is not None and input_path is not None:
        raise ValueError("Pass either final_df or input_path, not both")

    if final_df is None:
        source = input_path or (workspace.output_dir / "final_providers.csv")
        source = Path(source)
        if not source.exists():
            raise FileNotFoundError(source)
        final_df = pd.read_csv(source, dtype=str, keep_default_na=False)
    else:
        final_df = final_df.copy()

    required = {"state", "postal_code"}
    missing = required - set(final_df.columns)
    if missing:
        raise ValueError(f"Provider data is missing required ZIP input column(s): {sorted(missing)}")

    zip_df = final_df[["state", "postal_code"]].copy()
    zip_df["state"] = zip_df["state"].map(normalize_state)
    zip_df["zip"] = zip_df["postal_code"].map(normalize_zip)
    zip_df = (
        zip_df.loc[zip_df["state"].notna() & zip_df["zip"].notna(), ["state", "zip"]]
        .drop_duplicates()
        .sort_values(["state", "zip"], kind="stable")
        .reset_index(drop=True)
    )

    conflicts = (
        zip_df.groupby("zip", as_index=False)["state"]
        .agg(lambda values: ",".join(sorted(set(values))))
        .rename(columns={"state": "states"})
    )
    conflicts["n_states"] = conflicts["states"].str.count(",") + 1
    conflicts = conflicts.loc[conflicts["n_states"] > 1].reset_index(drop=True)

    long_path = workspace.output_dir / ZIP_LONG_FILENAME
    workbook_path = workspace.output_dir / ZIP_WORKBOOK_FILENAME
    conflicts_path = workspace.output_dir / ZIP_CONFLICTS_FILENAME

    zip_df.to_csv(long_path, index=False, encoding="utf-8-sig")
    conflicts.to_csv(conflicts_path, index=False, encoding="utf-8-sig")

    state_columns: dict[str, pd.Series] = {}
    for state in sorted(zip_df["state"].unique()):
        values = (
            zip_df.loc[zip_df["state"] == state, "zip"]
            .drop_duplicates()
            .sort_values(kind="stable")
            .reset_index(drop=True)
        )
        state_columns[state] = values

    wide_df = pd.DataFrame(state_columns)
    counts_df = (
        zip_df.groupby("state", as_index=False)
        .agg(n_zips=("zip", "nunique"))
        .sort_values("state")
        .reset_index(drop=True)
    )

    with pd.ExcelWriter(workbook_path, engine="openpyxl") as writer:
        wide_df.to_excel(writer, sheet_name=ZIP_SHEET_NAME, index=False)
        counts_df.to_excel(writer, sheet_name=ZIP_COUNTS_SHEET_NAME, index=False)

        # ZIPs must remain text so leading zeroes survive Excel edits/round-trips.
        worksheet = writer.book[ZIP_SHEET_NAME]
        for column in worksheet.iter_cols(min_row=2):
            for cell in column:
                if cell.value is not None:
                    cell.number_format = "@"

    return {
        "zip_workbook": workbook_path,
        "zip_long_csv": long_path,
        "zip_conflicts_csv": conflicts_path,
        "zip_rows": len(zip_df),
        "zip_states": int(zip_df["state"].nunique()) if len(zip_df) else 0,
        "zip_conflicts": len(conflicts),
    }


def _clean(value) -> str:
    if value is None:
        return ""
    if pd.api.types.is_scalar(value) and pd.isna(value):
        return ""
    return str(value).strip()
