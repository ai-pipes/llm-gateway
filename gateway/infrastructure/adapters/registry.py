from gateway.domain.adapters.base import BaseLLMAdapter


class AdapterRegistry:
    def __init__(self):
        self._adapters: dict[str, BaseLLMAdapter] = {}
        self._default: str | None = None

    def register(self, adapter: BaseLLMAdapter, default: bool = False) -> None:
        self._adapters[adapter.name] = adapter
        if default:
            self._default = adapter.name

    def get(self, name: str | None = None) -> BaseLLMAdapter:
        target = name or self._default
        if target is None or target not in self._adapters:
            raise KeyError(f"Adapter '{target}' not found. Registered: {list(self._adapters)}")
        return self._adapters[target]
