"""Match post content and configurable author exclusions."""


def author_is_excluded(author: str, task: dict) -> bool:
    # Multiple actors are stored with the same separator used by the extractor.
    names = {" ".join(name.split()).casefold() for name in [author, *author.split("、")]}
    return any(" ".join(name.split()).casefold() in names for name in task.get("excluded_authors", []) if name.strip())


def match_keywords(content: str, task: dict, author: str = "") -> tuple[bool, list[str]]:
    if author_is_excluded(author, task):
        return False, []
    text = content.casefold()
    if any(word.casefold() in text for word in task["excludes"]):
        return False, []
    hits = [word for word in task["keywords"] if word.casefold() in text]
    if not task["keywords"]:
        return True, []
    matched = len(hits) == len(task["keywords"]) if task["match_mode"] == "all" else bool(hits)
    return matched, hits
