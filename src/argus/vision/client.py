"""Cloud vision client (Gemini ``generateContent`` REST API by default).

On-demand only: this module is only ever invoked from report generation
(``argus report daily --vision``), never from the daemon or a scheduler.

Auth: the API key is read from the environment only (``GEMINI_API_KEY``
then ``GOOGLE_API_KEY``, via ``Config.api_key()``). It is never stored in
or read from the config file. Gemini's REST API accepts the key either as
an ``?key=`` query param or the ``x-goog-api-key`` header; we send it as a
header to avoid it leaking into logged URLs.

Failure handling: every network/parse failure (timeout, connection error,
HTTP error incl. 429 rate limits, malformed/empty response body) is caught
here and turned into a logged warning + ``None`` return. Callers (report
generation) must treat ``None`` as "no narrative available" and continue —
this must never raise up and abort report generation.
"""

from __future__ import annotations

import base64
import logging
from pathlib import Path

import httpx

logger = logging.getLogger("argus.vision.client")

DEFAULT_TIMEOUT_SECONDS = 30.0

PROMPT_TEMPLATE = (
    "You are summarizing a user's computer activity session for a personal "
    "activity log. The session was in the application \"{app}\" with window "
    "title(s): {titles}. Based on the attached screenshot(s) from this "
    "session, write a short (1-2 sentence) description of what the user "
    "appeared to be doing. Be concise and factual."
)


def _build_prompt(app: str, titles: list[str]) -> str:
    titles_str = ", ".join(titles) if titles else "(no title captured)"
    return PROMPT_TEMPLATE.format(app=app, titles=titles_str)


def _image_to_inline_part(path: str) -> dict | None:
    try:
        data = Path(path).read_bytes()
    except OSError as exc:
        logger.warning("Vision: could not read screenshot %s: %s", path, exc)
        return None
    encoded = base64.b64encode(data).decode("ascii")
    return {"inline_data": {"mime_type": "image/webp", "data": encoded}}


def build_endpoint_url(endpoint: str, model: str) -> str:
    """Build the full generateContent URL from config's base endpoint +
    model name, e.g.

        https://generativelanguage.googleapis.com/v1beta/models
        + gemini-2.5-flash-lite
        -> https://.../v1beta/models/gemini-2.5-flash-lite:generateContent
    """
    base = endpoint.rstrip("/")
    return f"{base}/{model}:generateContent"


def build_request(
    app: str,
    titles: list[str],
    image_paths: list[str],
    endpoint: str,
    model: str,
) -> tuple[str, dict]:
    """Build the (url, json_body) pair for a Gemini generateContent call,
    without sending it. Exposed separately so it can be exercised/tested
    without any network access.
    """
    url = build_endpoint_url(endpoint, model)
    parts: list[dict] = [{"text": _build_prompt(app, titles)}]
    for path in image_paths:
        part = _image_to_inline_part(path)
        if part is not None:
            parts.append(part)
    body = {"contents": [{"parts": parts}]}
    return url, body


def _extract_text(response_json: dict) -> str | None:
    try:
        candidates = response_json["candidates"]
        parts = candidates[0]["content"]["parts"]
        text = "".join(p.get("text", "") for p in parts).strip()
        return text or None
    except (KeyError, IndexError, TypeError) as exc:
        logger.warning("Vision: malformed response, could not extract text: %s", exc)
        return None


def generate_session_narrative(
    api_key: str,
    app: str,
    titles: list[str],
    image_paths: list[str],
    endpoint: str,
    model: str,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
) -> str | None:
    """Call the vision API for a single session and return a short
    narrative, or ``None`` on any failure (network, HTTP, rate limit,
    malformed response). Never raises.
    """
    if not image_paths:
        return None

    url, body = build_request(app, titles, image_paths, endpoint, model)

    try:
        response = httpx.post(
            url,
            json=body,
            headers={"x-goog-api-key": api_key, "Content-Type": "application/json"},
            timeout=timeout,
        )
        response.raise_for_status()
    except httpx.TimeoutException as exc:
        logger.warning("Vision: request timed out for app=%r: %s", app, exc)
        return None
    except httpx.HTTPStatusError as exc:
        status = exc.response.status_code
        if status == 429:
            logger.warning("Vision: rate limited (429) for app=%r", app)
        else:
            logger.warning(
                "Vision: HTTP error %s for app=%r: %s", status, app, exc.response.text[:200]
            )
        return None
    except httpx.HTTPError as exc:
        logger.warning("Vision: request failed for app=%r: %s", app, exc)
        return None
    except Exception as exc:  # noqa: BLE001 - last-resort guard: narrative
        # generation must never crash report generation, whatever goes wrong.
        logger.warning("Vision: unexpected error calling vision API for app=%r: %s", app, exc)
        return None

    try:
        response_json = response.json()
    except ValueError as exc:
        logger.warning("Vision: response was not valid JSON for app=%r: %s", app, exc)
        return None

    return _extract_text(response_json)
