"""Local hashed bag-of-words embedder for chat-only OpenAI-compatible APIs."""

from __future__ import annotations

import hashlib
import math
import re
from typing import Any, Dict, Optional, Sequence

from adalflow.core.model_client import ModelClient
from adalflow.core.types import Embedding, EmbedderOutput, ModelType

LOCAL_EMBEDDING_DIM = 256
_TOKEN_RE = re.compile(r"[A-Za-z0-9_]+|[\u4e00-\u9fff]")


def hash_embed_text(text: str, dim: int = LOCAL_EMBEDDING_DIM) -> list[float]:
    """Return an L2-normalized hashed bag-of-words vector."""
    tokens = _TOKEN_RE.findall((text or "").lower())
    if not tokens:
        tokens = [text or ""]
    vector = [0.0] * dim
    for token in tokens:
        digest = hashlib.blake2b(token.encode("utf-8"), digest_size=8).digest()
        index = int.from_bytes(digest[:4], "little") % dim
        sign = 1.0 if digest[4] & 1 == 0 else -1.0
        vector[index] += sign
    norm = math.sqrt(sum(value * value for value in vector)) or 1.0
    return [value / norm for value in vector]


class LocalHashEmbedderClient(ModelClient):
    """ModelClient that embeds text locally so query and document vectors match."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.sync_client = True
        self.async_client = True

    def init_sync_client(self) -> None:
        self.sync_client = True

    def init_async_client(self) -> None:
        self.async_client = True

    def convert_inputs_to_api_kwargs(
        self,
        input: Optional[Any] = None,
        model_kwargs: Dict = {},
        model_type: ModelType = ModelType.UNDEFINED,
    ) -> Dict:
        if model_type != ModelType.EMBEDDER:
            raise ValueError(
                f"LocalHashEmbedderClient only supports EMBEDDER, got {model_type}"
            )
        if isinstance(input, str):
            texts = [input]
        elif isinstance(input, Sequence):
            texts = ["" if item is None else str(item) for item in input]
        else:
            texts = ["" if input is None else str(input)]
        return {
            "input": texts,
            "dimensions": int(model_kwargs.get("dimensions", LOCAL_EMBEDDING_DIM)),
        }

    def call(self, api_kwargs: Dict = {}, model_type: ModelType = ModelType.UNDEFINED):
        texts = api_kwargs.get("input") or [""]
        dim = int(api_kwargs.get("dimensions", LOCAL_EMBEDDING_DIM))
        return {"embeddings": [hash_embed_text(text, dim) for text in texts]}

    async def acall(
        self, api_kwargs: Dict = {}, model_type: ModelType = ModelType.UNDEFINED
    ):
        return self.call(api_kwargs=api_kwargs, model_type=model_type)

    def parse_embedding_response(self, response: Any) -> EmbedderOutput:
        embeddings = []
        if isinstance(response, dict):
            embeddings = response.get("embeddings") or []
        elif isinstance(response, Sequence):
            embeddings = list(response)
        data = [
            Embedding(embedding=list(vector), index=index)
            for index, vector in enumerate(embeddings)
            if isinstance(vector, Sequence)
        ]
        return EmbedderOutput(data=data, model="local-hash", raw_response=response)
