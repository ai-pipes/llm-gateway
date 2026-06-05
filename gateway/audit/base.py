from abc import ABC, abstractmethod
from .record import AuditRecord


class BaseAuditBackend(ABC):
    @abstractmethod
    async def write(self, record: AuditRecord) -> None:
        ...
