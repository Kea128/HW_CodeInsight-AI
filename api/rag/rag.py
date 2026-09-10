import asyncio
import os
from collections import defaultdict
from collections.abc import Sized
from uuid import uuid4

import adalflow as adal
from adalflow.components.retriever.faiss_retriever import FAISSRetriever
from adalflow.core.types import (
    AssistantResponse,
    DialogTurn,
    Document,
    RetrieverOutput,
    UserQuery,
)

from api.config import configs, get_embedder
from api.logger import get_logger
from api.rag.pipeline import DatabaseManager

logger = get_logger(__name__)

NO_SOURCE_FILES_ERROR = (
    "同步副本里没有可分析的代码文件。请先完成同步，或添加子分析目录后再开始分析。"
)
NO_EMBEDDINGS_ERROR = (
    "无法为代码建立检索索引。自定义接口通常不提供嵌入模型。"
    "请在设置里把嵌入方式改为「不使用向量」，或改用本机 Ollama。"
)

# Maximum concurrent RAG preparing count
_RAG_PREPARE_SEMAPHORE: asyncio.Semaphore | None = None


def _ollama_model_matches(requested: str, available: str) -> bool:
    if requested == available:
        return True
    return ":" not in requested and available == f"{requested}:latest"


def _get_rag_semaphore() -> asyncio.Semaphore:
    global _RAG_PREPARE_SEMAPHORE
    if _RAG_PREPARE_SEMAPHORE is None:
        _RAG_PREPARE_SEMAPHORE = asyncio.Semaphore(
            int(os.environ.get("DEEPWIKI_MAX_CONCURRENT_RAG", "4"))
        )
    assert isinstance(_RAG_PREPARE_SEMAPHORE, asyncio.Semaphore)
    return _RAG_PREPARE_SEMAPHORE


def check_ollama_model_exists(model_name: str, ollama_host: str | None = None) -> bool:
    """
    Check if an Ollama model exists before attempting to use it.

    Args:
        model_name: Name of the model to check
        ollama_host: Ollama host URL, defaults to localhost:11434

    Returns:
        bool: True if model exists, False otherwise
    """
    import httpx
    import ollama

    if ollama_host is None:
        ollama_host = os.getenv("OLLAMA_HOST", "http://localhost:11434")
    try:
        # Remove /api prefix if present and add it back
        ollama_host = ollama_host.removesuffix("/api")
        ret: ollama.ListResponse = ollama.Client(host=ollama_host, timeout=5).list()
        is_available = any(
            _ollama_model_matches(model_name, model.model) for model in ret.models
        )
        if is_available:
            logger.info("Ollama model '%s' is available", model_name)
        else:
            logger.warning(
                "Ollama model '%s' is not available. Available models: %s. ",
                model_name,
                str([model.model for model in ret.models]),
            )
        return is_available
    except (httpx.ConnectTimeout, ConnectionError) as e:
        logger.warning(f"Could not connect to Ollama to check models: {e}")
        return False
    except Exception as e:
        logger.warning(f"Error checking Ollama model availability: {e}")
        return False


class CustomConversation(list[DialogTurn]):
    """Custom implementation of Conversation to fix the list assignment index out of range error"""


class Memory(adal.core.component.DataComponent):
    """Simple conversation management with a list of dialog turns."""

    def __init__(self):
        super().__init__()
        # Use our custom implementation instead of the original Conversation class
        self.current_conversation = CustomConversation()

    def call(self) -> dict:
        """Return the conversation history as a dictionary."""
        all_dialog_turns = {
            dialog_turn.id: dialog_turn for dialog_turn in self.current_conversation
        }
        logger.info(f"Returning {len(all_dialog_turns)} dialog turns from memory")
        return all_dialog_turns

    def add_dialog_turn(self, user_query: str, assistant_response: str) -> None:
        """
        Add a dialog turn to the conversation history.

        Args:
            user_query: The user's query
            assistant_response: The assistant's response

        """
        # Create a new dialog turn using our custom implementation
        dialog_turn = DialogTurn(
            id=str(uuid4()),
            user_query=UserQuery(query_str=user_query),
            assistant_response=AssistantResponse(response_str=assistant_response),
        )

        # Safely append the dialog turn
        self.current_conversation.append(dialog_turn)
        logger.info(
            "Successfully added dialog turn, now have %d turns",
            len(self.current_conversation),
        )


def _get_document_vector_size(document: Document) -> int | None:
    if hasattr(document.vector, "shape"):
        embedding_size = (
            document.vector.shape[0]
            if len(document.vector.shape) == 1
            else document.vector.shape[-1]
        )
    elif isinstance(document.vector, Sized):
        embedding_size = len(document.vector)
    else:
        embedding_size = None

    return embedding_size


class RAG(adal.Component):
    """RAG with one repo.
    If you want to load a new repos, call prepare_retriever(repo_url_or_path) first."""

    def __init__(self, provider="google", model=None, use_s3: bool = False):  # noqa: F841 - use_s3 is kept for compatibility
        """
        Initialize the RAG component.

        Args:
            provider: Model provider to use (google, openai, openrouter, ollama)
            model: Model name to use with the provider
            use_s3: Whether to use S3 for database storage (default: False)
        """
        super().__init__()

        self.provider = provider
        self.model = model

        # Import the helper functions
        from api.config import get_embedder_type

        # Determine embedder type based on current configuration
        self.embedder_type = get_embedder_type()
        self.is_ollama_embedder = (
            self.embedder_type == "ollama"
        )  # Backward compatibility

        # Check if Ollama model exists before proceeding
        if self.is_ollama_embedder:
            from api.config import get_embedder_config

            embedder_config = get_embedder_config()
            if embedder_config and embedder_config.get("model_kwargs", {}).get("model"):
                model_name = embedder_config["model_kwargs"]["model"]
                if not check_ollama_model_exists(model_name):
                    raise ValueError(
                        f"Ollama model '{model_name}' not found. Please run 'ollama pull {model_name}' to install it."
                    )

        # Initialize components
        self.memory = Memory()
        self.embedder = get_embedder(embedder_type=self.embedder_type)
        self.initialize_db_manager()

    def _switch_to_local_embedder(self) -> None:
        self.embedder_type = "none"
        self.is_ollama_embedder = False
        self.embedder = get_embedder(embedder_type="none")

    def _create_faiss_retriever(self) -> None:
        self.retriever = FAISSRetriever(
            **configs["retriever"],
            embedder=self.embedder,
            documents=self.transformed_docs,
            document_map_func=lambda doc: doc.vector,
        )
        logger.info("FAISS retriever created successfully")

    @staticmethod
    def _embed_documents_locally(documents: list[Document]) -> list[Document]:
        from api.clients.local_embedder import LOCAL_EMBEDDING_DIM, hash_embed_text

        for document in documents:
            document.vector = hash_embed_text(
                document.text or "", LOCAL_EMBEDDING_DIM
            )
        return documents

    def initialize_db_manager(self):
        """Initialize the database manager with local storage"""
        self.db_manager = DatabaseManager()
        self.transformed_docs = []

    @staticmethod
    def _validate_and_filter_embeddings(documents: list[Document]) -> list:
        """
        Validate embeddings and filter out documents with invalid or mismatched embedding sizes.

        Args:
            documents: List of documents with embeddings

        Returns:
            List of documents with valid embeddings of consistent size
        """
        if not documents:
            logger.warning("No documents provided for embedding validation")
            return []

        docs_embeddings = defaultdict(list)

        for doc, embed_size in filter(
            lambda x: isinstance(x[0], Document) and bool(x[1]),
            ((x, _get_document_vector_size(x)) for x in documents),
        ):
            docs_embeddings[embed_size].append(doc)
        if not docs_embeddings:
            logger.error("No valid embeddings found in any documents")
            return []

        target_size = max(docs_embeddings, key=lambda x: len(docs_embeddings[x]))
        logger.info(
            "Target embedding size: %s (found in %s documents)",
            target_size,
            len(docs_embeddings[target_size]),
        )

        valid_documents = docs_embeddings.pop(target_size)

        if docs_embeddings:
            for embed_size, docs_list in docs_embeddings.items():
                logger.warning(
                    "Found %s documents with incorrect embedding size %s, will be filtered out.",
                    len(docs_list),
                    str(embed_size),
                )

        if not valid_documents:
            logger.warning(
                "No documents with valid embeddings remained after filtering"
            )
        else:
            logger.info(
                "Embedding validation complete: %d/%d documents have valid embeddings.",
                len(valid_documents),
                len(documents),
            )
        return valid_documents

    def prepare_retriever(
        self,
        repo_url_or_path: str,
        type: str = "github",
        access_token: str | None = None,
        excluded_dirs: list[str] | None = None,
        excluded_files: list[str] | None = None,
        included_dirs: list[str] | None = None,
        included_files: list[str] | None = None,
        space_id: str | None = None,
    ):
        """
        Prepare the retriever for a repository.
        Will load database from local storage if available.

        Args:
            repo_url_or_path: URL or local path to the repository
            access_token: Optional access token for private repositories
            excluded_dirs: Optional list of directories to exclude from processing
            excluded_files: Optional list of file patterns to exclude from processing
            included_dirs: Optional list of directories to include exclusively
            included_files: Optional list of file patterns to include exclusively
        """
        self.initialize_db_manager()
        self.repo_url_or_path = repo_url_or_path
        self.transformed_docs = self.db_manager.prepare_database(
            repo_url_or_path,
            type,
            access_token,
            embedder_type=self.embedder_type,
            excluded_dirs=excluded_dirs,
            excluded_files=excluded_files,
            included_dirs=included_dirs,
            included_files=included_files,
            space_id=space_id,
        )
        logger.info(f"Loaded {len(self.transformed_docs)} documents for retrieval")
        used = getattr(self.db_manager, "embedder_type_used", None)
        if used == "none" and self.embedder_type != "none":
            self._switch_to_local_embedder()

        # Validate and filter embeddings to ensure consistent sizes
        valid_docs = self._validate_and_filter_embeddings(self.transformed_docs)
        if not valid_docs:
            if not self.transformed_docs:
                raise ValueError(NO_SOURCE_FILES_ERROR)
            logger.warning(
                "Remote embeddings missing; falling back to the local hash embedder"
            )
            try:
                from api.services.oplog import log_event

                log_event(
                    "embedder_fallback",
                    "上游嵌入不可用，已改用本地向量",
                    level="warn",
                    from_type=self.embedder_type,
                )
            except Exception:
                pass
            self._switch_to_local_embedder()
            self.transformed_docs = self._embed_documents_locally(self.transformed_docs)
            valid_docs = self._validate_and_filter_embeddings(self.transformed_docs)
        self.transformed_docs = valid_docs

        if not self.transformed_docs:
            raise ValueError(NO_EMBEDDINGS_ERROR)

        logger.info(
            f"Using {len(self.transformed_docs)} documents with valid embeddings for retrieval"
        )

        try:
            self._create_faiss_retriever()
        except Exception as e:
            message = str(e)
            logger.error(f"Error creating FAISS retriever: {message}")
            if self.embedder_type != "none" and (
                "No valid documents with embeddings" in message
                or "All embeddings should be of the same size" in message
            ):
                logger.warning(
                    "FAISS rejected remote embeddings; falling back to the local hash embedder"
                )
                try:
                    from api.services.oplog import log_event

                    log_event(
                        "embedder_fallback",
                        "上游嵌入无法建索引，已改用本地向量",
                        level="warn",
                        from_type=self.embedder_type,
                        error=message[:200],
                    )
                except Exception:
                    pass
                self._switch_to_local_embedder()
                self.transformed_docs = self._embed_documents_locally(
                    self.transformed_docs
                )
                self.transformed_docs = self._validate_and_filter_embeddings(
                    self.transformed_docs
                )
                if not self.transformed_docs:
                    raise ValueError(NO_EMBEDDINGS_ERROR) from e
                self._create_faiss_retriever()
                return
            if "No valid documents with embeddings" in message:
                raise ValueError(NO_EMBEDDINGS_ERROR) from e
            raise

    async def aprepare_retriever(
        self,
        repo_url_or_path: str,
        type: str = "github",
        access_token: str | None = None,
        excluded_dirs: list[str] | None = None,
        excluded_files: list[str] | None = None,
        included_dirs: list[str] | None = None,
        included_files: list[str] | None = None,
        space_id: str | None = None,
    ):
        """Async version of the original `prepare_retriever`.

        Reuse the synchronous `prepare_retriever` implementation, but runs it in
        a worker thread via `asyncio.to_thread` so that blocking operations (such
        as git.clone, file io, embedding calls) do not stall the outer event loop.
        Concurrency is bounded by a module-level semaphore, set by system variable
        'DEEPWIKI_MAX_CONCURRENT_RAG'.

        Args:
            repo_url_or_path: URL or local path to the repository
            access_token: Optional access token for private repositories
            excluded_dirs: Optional list of directories to exclude from processing
            excluded_files: Optional list of file patterns to exclude from processing
            included_dirs: Optional list of directories to include exclusively
            included_files: Optional list of file patterns to include exclusively
        """
        async with _get_rag_semaphore():
            return await asyncio.to_thread(
                self.prepare_retriever,
                repo_url_or_path,
                type=type,
                access_token=access_token,
                excluded_dirs=excluded_dirs,
                excluded_files=excluded_files,
                included_dirs=included_dirs,
                included_files=included_files,
                space_id=space_id,
            )

    def call(
        self, query: str | list[str], language: str = "en"
    ) -> list[RetrieverOutput]:
        """
        Process a query using RAG.

        Args:
            query: The user's query

        Returns:
            list of RetrieverOutput.
        """
        try:
            retrieved_documents = self.retriever(query)

            # Fill in the documents
            retrieved_documents[0].documents = [
                self.transformed_docs[doc_index]
                for doc_index in retrieved_documents[0].doc_indices
            ]

            return retrieved_documents

        except Exception:
            logger.exception("Error in RAG call, returning empty list")
            return []

    async def acall(self, query: str, language: str = "en") -> list[RetrieverOutput]:
        """Async version of the original `call` method."""
        return await asyncio.to_thread(self.call, query, language)
