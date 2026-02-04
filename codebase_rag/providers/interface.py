from __future__ import annotations

from abc import ABC, abstractmethod

from pydantic_ai.models import Model

from .. import constants as cs


class ModelProvider(ABC):
    def __init__(self, **config: str | int | None) -> None:
        self.config = config

    @abstractmethod
    def create_model(self, model_id: str, **kwargs: str | int | None) -> Model:
        pass

    @abstractmethod
    def validate_config(self) -> None:
        pass

    @property
    @abstractmethod
    def provider_name(self) -> cs.Provider | str:
        pass
