"""Workspace run lifecycle, matching the public competition UI API."""

from __future__ import annotations

import httpx2

from .config import Settings


async def start_run(settings: Settings) -> dict:
    """Open/retrieve the team's L3B run before requesting audited evidence."""
    async with httpx2.AsyncClient(timeout=30.0, follow_redirects=False) as client:
        response = await client.post(
            settings.competition_api_url + "/api/v2/runs",
            headers={"Authorization": f"Bearer {settings.team_api_key}"},
            json={"variant_id": "l3b"},
        )
    if response.status_code >= 300:
        raise RuntimeError(
            f"Competition run HTTP {response.status_code}; check key and competition phase"
        )
    try:
        run = response.json()
    except ValueError:
        raise ValueError("Competition did not return a JSON run") from None
    if not isinstance(run, dict):
        raise ValueError("Competition returned an invalid run")
    return run
