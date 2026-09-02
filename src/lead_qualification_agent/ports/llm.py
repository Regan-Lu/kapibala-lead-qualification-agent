from collections.abc import Mapping
from typing import Any, Protocol


class ModelServiceError(RuntimeError):
    """A remote model call or its structured output could not be used."""


class StructuredModelClient(Protocol):
    async def generate_json(
        self,
        *,
        system_instruction: str,
        user_input: str,
        response_schema: Mapping[str, Any],
    ) -> str:
        """Return one JSON document conforming to the requested schema."""
        ...
