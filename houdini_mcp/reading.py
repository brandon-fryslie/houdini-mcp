"""A tool result as its caller reads it, and whether that is too much to read whole.

[LAW:one-source-of-truth] The one line between a result read inline and one too
large to read whole. ``delivery`` hands a larger one over as a file, and
``summarization`` writes it the summary that is read in its place, so a result is
summarized exactly when it arrives as a file.
"""

import json
from dataclasses import dataclass
from typing import Any

from pydantic import TypeAdapter
from pydantic_core import PydanticSerializationError

from . import shorthand

RESPONSE_BOUND_BYTES = 16 * 1024
"""The largest result text, in UTF-8 bytes of its shorthand, that a caller reads inline."""

# [LAW:one-source-of-truth] The serializer FastMCP puts a result's structured content
# on the wire with, so the value rendered, measured against the bound, and written to
# a file is the result as structured content carries it.
WIRE = TypeAdapter(Any)


class Unreadable(TypeError):
    """A result the wire cannot carry as JSON, so no caller can read it."""


@dataclass(frozen=True)
class Reading:
    """A result as its caller reads it: the JSON value the wire carries, and that value's shorthand."""

    sent: dict[str, Any]
    text: str
    encoded: bytes
    """``text`` in UTF-8: the bytes measured against the bound, and written to a file."""

    @property
    def fits(self) -> bool:
        """Whether a caller reads it whole, inline: its text is within the bound."""
        return len(self.encoded) <= RESPONSE_BOUND_BYTES

    @classmethod
    def of(cls, result: dict[str, Any]) -> "Reading":
        """``result`` as its caller reads it.

        Raises:
            Unreadable: the wire cannot carry ``result`` as JSON.
        """
        try:
            sent: dict[str, Any] = json.loads(WIRE.dump_json(result))
        except PydanticSerializationError as e:
            raise Unreadable(
                f"the tool ran, but returned a result that is not JSON-serializable: {e}"
            ) from None
        text = shorthand.render(sent)
        return cls(sent, text, text.encode("utf-8"))
