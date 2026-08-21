import numpy as np
from adalflow.core.types import Document

from api.rag import RAG
from api.rag.rag import _ollama_model_matches


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
