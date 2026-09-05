from api.services.wiki.io import (
    delete_wiki_cache,
    export_wiki,
    get_wiki_cache_path,
    list_processed_projects,
    list_wiki_cache,
    read_wiki_cache,
    save_wiki_cache,
    wiki_cache_exists,
)

__all__ = [
    "export_wiki",
    "save_wiki_cache",
    "get_wiki_cache_path",
    "wiki_cache_exists",
    "read_wiki_cache",
    "delete_wiki_cache",
    "list_wiki_cache",
    "list_processed_projects",
    "WikiTask",
    "registry",
    "generate_repo_wiki",
    "wiki_task_store",
]


def __getattr__(name: str):
    if name in {"WikiTask", "registry", "generate_repo_wiki", "wiki_task_store"}:
        from api.services.wiki import tasks

        mapping = {
            "WikiTask": tasks.WikiTask,
            "registry": tasks.registry,
            "generate_repo_wiki": tasks.generate_repo_wiki,
            "wiki_task_store": tasks.wiki_task_store,
        }
        return mapping[name]
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
