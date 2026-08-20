import pytest
from pydantic import BaseModel

from api.schemas.io import aload, asave


class ExampleModel(BaseModel):
    value: str


@pytest.mark.asyncio
async def test_asave_atomically_replaces_existing_file(tmp_path):
    target = tmp_path / "model.json"
    target.write_text('{"value":"old"}', encoding="utf-8")

    await asave(ExampleModel(value="new"), str(target))

    assert await aload(ExampleModel, str(target)) == ExampleModel(value="new")
    assert list(tmp_path.glob("*.tmp")) == []
