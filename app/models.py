"""Match configurable keywords and exclusion words against a post body."""


def match_keywords(content: str, task: dict) -> tuple[bool, list[str]]:
    text = content.casefold()
    if any(word.casefold() in text for word in task["excludes"]):
        return False, []
    hits = [word for word in task["keywords"] if word.casefold() in text]
    if not task["keywords"]:
        return True, []
    matched = len(hits) == len(task["keywords"]) if task["match_mode"] == "all" else bool(hits)
    return matched, hits
