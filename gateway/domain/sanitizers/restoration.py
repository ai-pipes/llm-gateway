from __future__ import annotations
import secrets


class RestorationContext:
    def __init__(self) -> None:
        self._map: dict[str, str] = {}      # placeholder -> original
        self._reverse: dict[str, str] = {}  # original -> placeholder (dedup)

    def register(self, original: str, entity_type: str) -> str:
        if original in self._reverse:
            return self._reverse[original]
        placeholder = f"[{entity_type.upper()}_{secrets.token_hex(4)}]"
        self._map[placeholder] = original
        self._reverse[original] = placeholder
        return placeholder

    def restore(self, text: str) -> str:
        for placeholder, original in self._map.items():
            text = text.replace(placeholder, original)
        return text

    def has_replacements(self) -> bool:
        return bool(self._map)

    def build_system_instruction(self) -> str:
        return (
            "IMPORTANT: Some values in this message have been replaced with opaque placeholder "
            "tokens matching the pattern [TYPENAME_HEXCHARS] (e.g. [EMAIL_ADDRESS_3f2a1b0c], "
            "[PHONE_NUMBER_9d4e7a12]). These are NOT real values — do not invent, guess, or "
            "reconstruct the originals. Preserve every such token exactly as written — do not "
            "modify, translate, paraphrase, or remove them."
        )


class StreamingRestorer:
    """Restores PII placeholders in a streamed text with minimal output delay.

    Buffers only the bytes that could be the start of a placeholder ([...]).
    Everything else is passed through immediately, preserving true TTFF.
    """

    def __init__(self, context: RestorationContext) -> None:
        self._map: dict[str, str] = dict(context._map)  # snapshot: placeholder -> original
        self._buffer: str = ""
        self._max_len: int = max((len(k) for k in self._map), default=0)

    def feed(self, chunk: str) -> str:
        """Process a new chunk; return text safe to send to the client immediately."""
        self._buffer += chunk
        return self._drain()

    def finalize(self) -> str:
        """Flush whatever remains in the buffer at end of stream."""
        out = self._buffer
        self._buffer = ""
        return out

    def _drain(self) -> str:
        out = ""
        while self._buffer:
            idx = self._buffer.find("[")
            if idx == -1:
                out += self._buffer
                self._buffer = ""
                break
            # Flush everything before the potential placeholder start
            out += self._buffer[:idx]
            self._buffer = self._buffer[idx:]
            result = self._probe()
            if result is None:
                break  # need more data
            out += result
        return out

    def _probe(self) -> str | None:
        """Called when self._buffer starts with '['.

        Returns:
            str  — text to emit (buffer advanced past consumed bytes)
            None — incomplete placeholder prefix, need more data
        """
        buf = self._buffer
        # Check if buffer starts with a complete registered placeholder
        for placeholder, original in self._map.items():
            if buf.startswith(placeholder):
                self._buffer = buf[len(placeholder):]
                return original
        # Check if buffer is still a valid prefix of some placeholder
        if any(p.startswith(buf) for p in self._map):
            if len(buf) > self._max_len:
                # Safety guard: buffer exceeds max placeholder length — give up
                self._buffer = buf[1:]
                return "["
            return None  # wait for more chunks
        # Not a prefix of any placeholder — flush the [ and continue
        self._buffer = buf[1:]
        return "["
