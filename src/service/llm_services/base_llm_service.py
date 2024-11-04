from abc import ABC, abstractmethod


class BaseLLMService(ABC):
    @abstractmethod
    def read():
        pass

    @abstractmethod
    def system_context():
        pass
