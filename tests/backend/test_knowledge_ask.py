from api.schemas import WikiPage
from api.services.knowledge.search import search_wiki_pages


def test_search_wiki_pages_ranks_overlapping_terms():
    pages = {
        "auth": WikiPage(
            id="auth",
            title="认证流程",
            content="登录使用 JWT，入口在 auth/service.py",
            filePaths=["auth/service.py"],
            importance="high",
            relatedPages=[],
        ),
        "ui": WikiPage(
            id="ui",
            title="界面布局",
            content="桌面设置抽屉",
            filePaths=["desktop-ui/app.js"],
            importance="low",
            relatedPages=[],
        ),
    }
    hits = search_wiki_pages(pages, "认证 JWT 在哪里")
    assert hits[0].id == "auth"
