"""Safely test read-only access to a Canvas API account.

Usage:
    python test_canvas_api.py https://canvas.example.edu

The script reads API_KEY from .env and requests only the current user's
profile. It never prints the token.
"""

from __future__ import annotations

import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener


def load_env(path: Path) -> None:
    """Load simple KEY=VALUE entries without adding a dependency."""
    if not path.exists():
        return

    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def save_response(endpoint: str, payload: object) -> Path:
    """Save API data locally, without persisting authorization credentials."""
    data_dir = Path(__file__).with_name("data")
    data_dir.mkdir(mode=0o700, exist_ok=True)
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    output_path = data_dir / f"{endpoint}_{timestamp}.json"
    record = {
        "retrieved_at_utc": datetime.now(timezone.utc).isoformat(),
        "endpoint": endpoint,
        "data": payload,
    }
    output_path.write_text(json.dumps(record, indent=2) + "\n", encoding="utf-8")
    try:
        output_path.chmod(0o600)
    except OSError:
        pass  # Windows file permissions are managed through ACLs.
    return output_path


class RejectRedirects(HTTPRedirectHandler):
    """Avoid sending an authorization header to an unexpected URL."""

    def redirect_request(self, request, fp, code, message, headers, newurl):  # type: ignore[no-untyped-def]
        return None


def next_page_url(link_header: str, base_url: str) -> str | None:
    """Return Canvas's next-page URL only when it remains on the same site."""
    match = re.search(r'<([^>]+)>;\s*rel="?next"?', link_header)
    if not match:
        return None
    candidate = match.group(1)
    base = urlsplit(base_url)
    parsed = urlsplit(candidate)
    if parsed.scheme != "https" or parsed.netloc != base.netloc:
        raise ValueError("Canvas returned a pagination URL outside the requested HTTPS site.")
    return candidate


def fetch_courses(base_url: str, token: str) -> list[object]:
    """Fetch all active courses available to the authenticated user."""
    next_url: str | None = (
        f"{base_url}/api/v1/courses?enrollment_state=active&per_page=100&include[]=term"
    )
    courses: list[object] = []
    opener = build_opener(RejectRedirects())
    while next_url:
        request = Request(
            next_url,
            headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
        )
        with opener.open(request, timeout=15) as response:
            page = json.load(response)
            if not isinstance(page, list):
                raise ValueError("Canvas returned an unexpected courses response.")
            courses.extend(page)
            next_url = next_page_url(response.headers.get("Link", ""), base_url)
    return courses


def main() -> int:
    load_env(Path(__file__).with_name(".env"))
    if len(sys.argv) > 3:
        print("Usage: python test_canvas_api.py [https://your-school.instructure.com] [profile|courses]")
        return 2
    base_url = (sys.argv[1] if len(sys.argv) >= 2 else os.getenv("CANVAS_BASE_URL", "")).rstrip("/")
    action = sys.argv[2] if len(sys.argv) == 3 else "profile"
    token = os.getenv("API_KEY", "")

    if not base_url:
        print("Usage: python test_canvas_api.py https://your-school.instructure.com [profile|courses]")
        print("Or add CANVAS_BASE_URL to .env.")
        return 2
    if not token:
        print("API_KEY is missing. Add it to .env.")
        return 2
    url = urlsplit(base_url)
    if url.scheme != "https" or not url.netloc or url.username or url.password or url.query or url.fragment:
        print("Canvas URL must be a plain HTTPS URL, such as https://your-school.instructure.com")
        return 2

    try:
        if action == "courses":
            courses = fetch_courses(base_url, token)
            saved_path = save_response("courses", courses)
            print(f"Retrieved {len(courses)} active course(s).")
            print(f"Response saved locally to: {saved_path.relative_to(Path.cwd())}")
            return 0
        if action != "profile":
            print("Action must be either 'profile' or 'courses'.")
            return 2

        request = Request(
            f"{base_url}/api/v1/users/self/profile",
            headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
        )
        with build_opener(RejectRedirects()).open(request, timeout=15) as response:
            profile = json.load(response)
    except HTTPError as error:
        if error.code == 401:
            print("Authentication failed (401): the API key is invalid or expired.")
        elif error.code == 403:
            print("Access denied (403): the API key lacks permission for this Canvas site.")
        else:
            print(f"Canvas returned HTTP {error.code}: {error.reason}")
        return 1
    except (URLError, ValueError) as error:
        print(f"Could not reach Canvas: {error.reason}")
        return 1

    print("Canvas API connection succeeded.")
    print(f"User: {profile.get('name', 'Unknown')} (ID: {profile.get('id', 'Unknown')})")
    print(f"Login: {profile.get('login_id', 'Unknown')}")
    saved_path = save_response("profile", profile)
    print(f"Response saved locally to: {saved_path.relative_to(Path.cwd())}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
