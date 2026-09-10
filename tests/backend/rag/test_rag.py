import numpy as np
import pytest
from adalflow.core.types import Document

from api.config import get_embedder, get_embedder_type
from api.clients.local_embedder import LOCAL_EMBEDDING_DIM, hash_embed_text
from api.rag import RAG
from api.rag.rag import NO_SOURCE_FILES_ERROR, _ollama_model_matches


def test_rag_valid_filter_documents():
    doc_list = [
        Document(
            text="test1",
            vector=[10, 11, 12],
            meta_data={},
        ),
        Document(
            text="test2",
            vector=np.array([10, 11, 12]),
            meta_data={},
        ),
        Document(
            text="test3",
            vector=(10, 11, 12),
            meta_data={},
        ),
        Document(
            text="invalid1",
            vector=np.array([10, 11, 12, 13]),
            meta_data={},
        ),
        Document(
            text="invalid2",
            vector=None,
            meta_data={},
        ),
    ]

    validated_docs = doc_list.copy()[:3]
    assert validated_docs == RAG._validate_and_filter_embeddings(doc_list)


def test_ollama_model_match_accepts_implicit_latest_tag():
    assert _ollama_model_matches("nomic-embed-text", "nomic-embed-text:latest")
    assert _ollama_model_matches("qwen3:1.7b", "qwen3:1.7b")
    assert not _ollama_model_matches("qwen3:1.7b", "qwen3:4b")


def test_local_hash_embedder_is_stable_and_token_sensitive():
    first = hash_embed_text("def connect_ubuntu(): pass")
    second = hash_embed_text("def connect_ubuntu(): pass")
    other = hash_embed_text("unrelated vegetable soup recipe")
    assert first == second
    assert len(first) == LOCAL_EMBEDDING_DIM
    overlap = sum(a * b for a, b in zip(first, hash_embed_text("connect_ubuntu helper")))
    other_score = sum(a * b for a, b in zip(first, other))
    assert overlap > other_score


def test_get_embedder_none_does_not_need_an_api_key():
    embedder = get_embedder(embedder_type="none")
    output = embedder(input="hello from local embedder")
    assert output.data
    assert len(output.data[0].embedding) == LOCAL_EMBEDDING_DIM


def test_get_embedder_type_honors_none(monkeypatch):
    import api.config as config

    monkeypatch.setattr(config, "EMBEDDER_TYPE", "none")
    assert get_embedder_type() == "none"


def test_prepare_retriever_reports_empty_repo_in_chinese(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    monkeypatch.setattr(
        "api.rag.rag.DatabaseManager.prepare_database",
        lambda self, *args, **kwargs: [],
    )
    rag = RAG(provider="openai")
    with pytest.raises(ValueError, match="没有可分析的代码文件"):
        rag.prepare_retriever("/tmp/empty-repo")
    assert str(NO_SOURCE_FILES_ERROR).startswith("同步副本")


def test_prepare_retriever_falls_back_when_embeddings_empty(monkeypatch):
    docs = [
        Document(
            text="def analyze_remote():\n    return True\n",
            vector=None,
            meta_data={},
        ),
        Document(text="class RemoteProject:\n    pass\n", vector=[], meta_data={}),
    ]

    def fake_prepare(self, *args, **kwargs):
        self.embedder_type_used = "openai"
        return docs

    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    monkeypatch.setattr(
        "api.rag.rag.DatabaseManager.prepare_database",
        fake_prepare,
    )
    rag = RAG(provider="openai")
    rag.prepare_retriever("/tmp/remote-mirror")

    assert rag.embedder_type == "none"
    assert rag.transformed_docs
    assert all(len(document.vector) == LOCAL_EMBEDDING_DIM for document in rag.transformed_docs)
    assert rag.retriever is not None
