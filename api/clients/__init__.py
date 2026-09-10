"""Unify Adalflow compatible clients to here.
Any patches and additional clients could be applied or imported in this module.
"""

from api.desktop_runtime import configure_runtime

configure_runtime()

from adalflow.components.model_client import (
    AzureAIClient,
    GoogleGenAIClient,
    OpenAIClient,
)

from .anthropic import AnthropicBedrockClient
from .bedrock import BedrockClient
from .dashscope import DashscopeClient
from .google_embedder import GoogleEmbedderClient
from .litellm import LiteLLMClient
from .local_embedder import LocalHashEmbedderClient
from .ollama import OllamaClient
from .openai_compat import CompatibleChatClient
from .openrouter import OpenRouterClient

__all__ = [
    "AnthropicBedrockClient",
    "AzureAIClient",
    "BedrockClient",
    "CompatibleChatClient",
    "DashscopeClient",
    "GoogleEmbedderClient",
    "GoogleGenAIClient",
    "LiteLLMClient",
    "LocalHashEmbedderClient",
    "OllamaClient",
    "OpenAIClient",
    "OpenRouterClient",
]
