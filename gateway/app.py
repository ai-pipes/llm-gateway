# gateway/app.py
import importlib
import os

from fastapi import FastAPI

from gateway.application.chat_service import ChatService
from gateway.api.openai.routes import create_router
from gateway.domain.audit.base import BaseAuditBackend
from gateway.domain.sanitizers.base import SanitizerChain
from gateway.infrastructure.adapters.registry import AdapterRegistry
from gateway.infrastructure.adapters.openai_compatible import OpenAICompatibleAdapter
from gateway.infrastructure.audit.file_backend import FileAuditBackend
from gateway.infrastructure.audit.stdout_backend import StdoutAuditBackend
from gateway.infrastructure.auth.base import BaseAuthProvider
from gateway.infrastructure.auth.middleware import AuthMiddleware


def create_app_from_components(
    auth_provider: BaseAuthProvider,
    input_chain: SanitizerChain,
    output_chain: SanitizerChain,
    audit_backend: BaseAuditBackend,
    registry: AdapterRegistry,
    log_body: bool = False,
) -> FastAPI:
    """Assemble app from pre-built components. Used in tests and for DI."""
    app = FastAPI(title="LLM Gateway", version="0.1.0")

    chat_service = ChatService(
        input_chain=input_chain,
        output_chain=output_chain,
        registry=registry,
        audit=audit_backend,
        log_body=log_body,
    )

    app.add_middleware(AuthMiddleware, provider=auth_provider)
    app.include_router(create_router(chat_service))
    return app


def create_app(config_path: str = "gateway.yaml") -> FastAPI:
    """Assemble app from config file. Used for production startup."""
    from gateway.config import load_config

    config = load_config(config_path)

    # Auth provider
    module_path, class_name = config.auth.module.rsplit(".", 1)
    module = importlib.import_module(module_path)
    auth_provider: BaseAuthProvider = getattr(module, class_name)(**config.auth.config)

    # Sanitizer chains
    def _load_chain(confs: list) -> SanitizerChain:
        sanitizers = []
        for s_conf in confs:
            try:
                mod_path, cls_name = s_conf.module.rsplit(".", 1)
                mod = importlib.import_module(mod_path)
                cls = getattr(mod, cls_name)
                sanitizers.append(cls(**s_conf.config))
            except (ImportError, AttributeError, ValueError, TypeError) as e:
                raise ValueError(f"Cannot load sanitizer '{s_conf.module}': {e}") from e
        return SanitizerChain(sanitizers)

    input_chain = _load_chain(config.sanitizers.input)
    output_chain = _load_chain(config.sanitizers.output)

    # Audit backend
    match config.audit.type:
        case "stdout":
            audit_backend: BaseAuditBackend = StdoutAuditBackend()
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
            api_key = os.environ.get(adapter_conf.auth.token_env, "")
            adapter = OpenAICompatibleAdapter(
                name=adapter_conf.name,
                base_url=adapter_conf.base_url,
                api_key=api_key,
                include_stream_usage=adapter_conf.include_stream_usage,
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
        log_body=config.audit.body_logging.enabled,
    )
