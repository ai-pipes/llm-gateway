import importlib
import os
from fastapi import FastAPI
from gateway.adapters.base import BaseLLMAdapter
from gateway.adapters.registry import AdapterRegistry
from gateway.audit.base import BaseAuditBackend
from gateway.audit.file_backend import FileAuditBackend
from gateway.audit.stdout_backend import StdoutAuditBackend
from gateway.middleware.auth import AuthMiddleware, BaseAuthProvider
from gateway.middleware.sanitize import SanitizeMiddleware
from gateway.middleware.audit import AuditLogMiddleware
from gateway.sanitizers.base import SanitizerChain
from gateway.routes import create_router


def create_app_from_components(
    auth_provider: BaseAuthProvider,
    input_chain: SanitizerChain,
    output_chain: SanitizerChain,
    audit_backend: BaseAuditBackend,
    registry: AdapterRegistry,
) -> FastAPI:
    """Собрать приложение из готовых компонентов. Используется в тестах и для DI."""
    app = FastAPI(title="LLM Gateway", version="0.1.0")

    # Middleware add order: last added = outermost (first to process request).
    # Execution order: AuthMiddleware → SanitizeMiddleware → AuditLogMiddleware → Route
    # - Auth rejects unauthenticated requests before anything else (no audit written).
    # - Sanitize runs before Audit: Audit only ever sees sanitized data.
    # - Audit wraps Route: writes record for every authenticated request including blocked ones.
    app.add_middleware(AuditLogMiddleware, backend=audit_backend)
    app.add_middleware(SanitizeMiddleware, input_chain=input_chain, output_chain=output_chain)
    app.add_middleware(AuthMiddleware, provider=auth_provider)

    app.include_router(create_router(registry))
    return app


def create_app(config_path: str = "gateway.yaml") -> FastAPI:
    """Собрать приложение из конфиг-файла. Используется для запуска в production."""
    from gateway.config import load_config

    config = load_config(config_path)

    # Auth provider
    module_path, class_name = config.auth.module.rsplit(".", 1)
    module = importlib.import_module(module_path)
    auth_provider_cls = getattr(module, class_name)
    auth_provider = auth_provider_cls(**config.auth.config)

    # Wire sanitizer chains from config
    def _load_chain(confs):
        sanitizers = []
        for s_conf in confs:
            try:
                mod_path, cls_name = s_conf.module.rsplit(".", 1)
                mod = importlib.import_module(mod_path)
                cls = getattr(mod, cls_name)
                # CONTRACT: sanitizer __init__ must accept **config as keyword arguments
                sanitizers.append(cls(**s_conf.config))
            except (ImportError, AttributeError, ValueError, TypeError) as e:
                raise ValueError(f"Cannot load sanitizer '{s_conf.module}': {e}") from e
        return SanitizerChain(sanitizers)

    input_chain = _load_chain(config.sanitizers.input)
    output_chain = _load_chain(config.sanitizers.output)

    # Audit backend
    match config.audit.type:
        case "stdout":
            audit_backend = StdoutAuditBackend()
        case "file":
            audit_backend = FileAuditBackend(path=config.audit.path)
        case "plugin":
            try:
                mod_path, cls_name = config.audit.module.rsplit(".", 1)
                mod = importlib.import_module(mod_path)
                cls = getattr(mod, cls_name)
                audit_backend = cls(**config.audit.config)
            except (ImportError, AttributeError, ValueError, TypeError) as e:
                raise ValueError(f"Cannot load audit backend '{config.audit.module}': {e}") from e
        case _:
            raise ValueError(f"Unknown audit backend type: {config.audit.type!r}")

    # Adapter registry
    registry = AdapterRegistry()
    for adapter_conf in config.adapters:
        if adapter_conf.type == "openai_compatible":
            from gateway.adapters.openai_compatible import OpenAICompatibleAdapter
            api_key = os.environ.get(adapter_conf.auth.token_env, "")
            adapter: BaseLLMAdapter = OpenAICompatibleAdapter(
                name=adapter_conf.name,
                base_url=adapter_conf.base_url,
                api_key=api_key,
            )
        elif adapter_conf.type == "plugin":
            mod_path, cls_name = adapter_conf.module.rsplit(".", 1)
            mod = importlib.import_module(mod_path)
            adapter = getattr(mod, cls_name)()
        else:
            continue
        registry.register(adapter, default=adapter_conf.default)

    return create_app_from_components(
        auth_provider=auth_provider,
        input_chain=input_chain,
        output_chain=output_chain,
        audit_backend=audit_backend,
        registry=registry,
    )
