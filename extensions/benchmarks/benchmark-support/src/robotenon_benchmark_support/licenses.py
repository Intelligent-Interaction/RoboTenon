"""Explicit asset-license gate for benchmark-owned downloads."""
import os


def ensure_license(license_id: str, *, url: str, description: str) -> None:
    accepted = {v.strip() for v in os.environ.get(
        "ROBOTENON_ACCEPTED_LICENSES", "").split(",")}
    if license_id not in accepted:
        raise PermissionError(
            f"{description}: review {url}; after accepting, include {license_id} "
            "in ROBOTENON_ACCEPTED_LICENSES. No assets have been downloaded."
        )
