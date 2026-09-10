"""OpenAI-compatible chat client using /v1/chat/completions.

adalflow's OpenAIClient defaults to https://api.openai.com/v1 and the
Responses API. Custom / internal gateways only implement Chat Completions,
so desktop "openai_compatible" must use this client.
"""

from __future__ import annotations

import os
from typing import Any, Dict, Optional, Sequence

from adalflow.core.types import ModelType

from adalflow.components.model_client.openai_client import OpenAIClient

from api.services.model_discover import normalize_openai_base_url


def resolved_openai_base_url() -> str:
    raw = (os.environ.get("OPENAI_BASE_URL") or "").strip()
    if not raw:
        try:
            from api.desktop_settings import load_desktop_settings

            raw = (load_desktop_settings().get("base_url") or "").strip()
        except Exception:
            raw = ""
    if not raw:
        raise ValueError("未配置自定义 API 地址")
    return normalize_openai_base_url(raw)


class CompatibleChatClient(OpenAIClient):
    """Chat Completions client pointed at the desktop custom API."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        if not kwargs.get("base_url"):
            kwargs["base_url"] = resolved_openai_base_url()
        super().__init__(*args, **kwargs)

    def convert_inputs_to_api_kwargs(
        self,
        input: Optional[Any] = None,
        model_kwargs: Dict = {},
        model_type: ModelType = ModelType.UNDEFINED,
    ) -> Dict:
        if model_type == ModelType.EMBEDDER:
            return super().convert_inputs_to_api_kwargs(
                input=input, model_kwargs=model_kwargs, model_type=model_type
            )
        if model_type not in {ModelType.LLM, ModelType.LLM_REASONING}:
            raise ValueError(f"model_type {model_type} is not supported")
        final = dict(model_kwargs)
        if isinstance(input, list) and input and isinstance(input[0], dict):
            messages = input
        elif isinstance(input, str):
            messages = [{"role": "user", "content": input}]
        elif isinstance(input, Sequence):
            messages = [{"role": "user", "content": str(item)} for item in input]
        else:
            messages = [{"role": "user", "content": "" if input is None else str(input)}]
        final["messages"] = messages
        final.pop("input", None)
        return final

    def call(self, api_kwargs: Dict = {}, model_type: ModelType = ModelType.UNDEFINED):
        self._api_kwargs = api_kwargs
        if model_type == ModelType.EMBEDDER:
            return self.sync_client.embeddings.create(**api_kwargs)
        if model_type not in {ModelType.LLM, ModelType.LLM_REASONING}:
            raise ValueError(f"model_type {model_type} is not supported")
        return self.sync_client.chat.completions.create(**api_kwargs)

    async def acall(
        self, api_kwargs: Dict = {}, model_type: ModelType = ModelType.UNDEFINED
    ):
        self._api_kwargs = api_kwargs
        if self.async_client is None:
            self.async_client = self.init_async_client()
        if model_type == ModelType.EMBEDDER:
            return await self.async_client.embeddings.create(**api_kwargs)
        if model_type not in {ModelType.LLM, ModelType.LLM_REASONING}:
            raise ValueError(f"model_type {model_type} is not supported")
        return await self.async_client.chat.completions.create(**api_kwargs)
