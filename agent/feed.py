"""Pull newly seen jobs from the melbourne-tech-jobs /feed/jobs endpoint."""
import httpx

from agent.store import Store

CURSOR_KEY = "feed_cursor"


def sync(store: Store, feed_url: str, client: httpx.Client, page_size: int = 200, max_pages: int = 50) -> int:
    """Fetch every job first seen since the stored cursor. Safe to re-run."""
    cursor = store.get_meta(CURSOR_KEY)
    total = 0
    for _ in range(max_pages):
        params = {"limit": page_size}
        if cursor:
            params.update(since=cursor["since"], after_id=cursor["after_id"])
        response = client.get(feed_url, params=params, timeout=30)
        response.raise_for_status()
        body = response.json()
        if body["items"]:
            total += store.save_jobs(body["items"])
        if body.get("next_cursor"):
            cursor = body["next_cursor"]
            # Advance only after the page is stored, so a crash re-fetches instead of skipping.
            store.set_meta(CURSOR_KEY, cursor)
        if not body.get("has_more"):
            break
    return total
