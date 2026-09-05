import re

from api.schemas import WikiPage


def tokenize(text: str) -> set[str]:
    return {token for token in re.findall(r"[\w\u4e00-\u9fff]{2,}", text.lower())}


def search_wiki_pages(
    pages: dict[str, WikiPage], question: str, limit: int = 4
) -> list[WikiPage]:
    terms = tokenize(question)
    scored: list[tuple[int, WikiPage]] = []
    for page in pages.values():
        haystack = tokenize(f"{page.title} {page.content}")
        score = len(terms & haystack)
        if score:
            scored.append((score, page))
    scored.sort(key=lambda item: item[0], reverse=True)
    return [page for _, page in scored[:limit]]
