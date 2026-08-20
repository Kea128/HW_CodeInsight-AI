import os
import uuid
from typing import TypeVar

import anyio
from pydantic import BaseModel

_M = TypeVar("_M", bound=BaseModel)


async def asave(model: BaseModel, path: str, *, encoding: str = "utf-8"):
    """Serialize a model and atomically replace the destination file."""

    temporary_path = f"{path}.{uuid.uuid4().hex}.tmp"
    try:
        async with await anyio.open_file(
            temporary_path, mode="w", encoding=encoding
        ) as file:
            await file.write(model.model_dump_json())
            await file.flush()
        await anyio.to_thread.run_sync(os.replace, temporary_path, path)
    finally:
        try:
            await anyio.to_thread.run_sync(os.remove, temporary_path)
        except OSError:
            pass


async def aload(model: type[_M], path: str, *, encoding: str = "utf-8") -> _M:
    """Asynchronous deserialize and load a model"""

    async with await anyio.open_file(path, mode="r", encoding=encoding) as file:
        return model.model_validate_json(await file.read())
