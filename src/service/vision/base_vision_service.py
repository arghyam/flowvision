from abc import ABC, abstractmethod


class BaseVisionService(ABC):
    @abstractmethod
    def read():
        pass

    @abstractmethod
    def system_context():
        pass
