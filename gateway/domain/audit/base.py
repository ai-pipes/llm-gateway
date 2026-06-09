from abc import ABC, abstractmethod
from gateway.domain.models import AuditRecord


class BaseAuditBackend(ABC):
    @abstractmethod
    async def write(self, record: AuditRecord) -> None:
        ...
