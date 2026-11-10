"""Normalize Facebook post links without carrying tracking parameters."""
import re
from urllib.parse import parse_qs, urlparse


def canonical_post_url(value: str) -> tuple[str, str]:
    parts = urlparse(value)
    if parts.scheme != "https" or parts.hostname not in {"www.facebook.com", "m.facebook.com", "facebook.com"}:
        return "", ""
    match = re.search(r"(/groups/[^/]+/(?:posts|permalink)/([^/?#]+)|/[^/]+/posts/([^/?#]+))", parts.path)
    if match:
        return "https://www.facebook.com" + match[1].rstrip("/") + "/", match[2] or match[3]
    query = parse_qs(parts.query)
    if parts.path in {"/permalink.php", "/story.php"} and query.get("story_fbid") and query.get("id"):
        post_id, author = query["story_fbid"][0], query["id"][0]
        if re.fullmatch(r"[A-Za-z0-9_]+", post_id) and author.isdigit():
            return f"https://www.facebook.com/permalink.php?story_fbid={post_id}&id={author}", post_id
    return "", ""
