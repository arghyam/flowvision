from abc import ABC, abstractmethod


class BaseVisionService(ABC):
    @abstractmethod
    def extract(image=None, image_bytes: bytes | None = None, download_url: str | None = None):
        pass

    @abstractmethod
    def system_context():
        pass
