from openai import OpenAI, OpenAIError
import base64

from error.error import CustomHTTPException, ErrorCode
from service.vision.base_vision_service import BaseVisionService
from conf.config import Config

import os
import logging


class OpenAIVisionService(BaseVisionService):
    def __init__(self, config: Config):
        api_logger_name = config.find("logs.api_logger.name")
        self.logger = logging.getLogger(api_logger_name)
        self.logger.info("Creating OpenAI client...")
        self.openai_client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))


    # Will need to change to just use url
    def extract(self, image=None, image_bytes: bytes | None = None, download_url: str | None = None) -> str:
        try:

            content_messages = []
            content_messages.append({"type": "text", "text": "Extract the meter readings from the image"})

            if download_url:
                content_messages.append({"type": "image_url", "image_url": {"url": download_url}})
            elif image:
                content_messages.append({"type": "image", "image": image})
            elif image_bytes:
                base64_image = base64.b64encode(image_bytes).decode('utf-8')
                url = f"data:image/png;base64,{base64_image}"
                content_messages.append({"type": "image_url", "image_url": {"url": url}})

            messages = []
            messages.append({"role": "system", "content": [{"type": "text", "text": self.system_context()}]})
            messages.append({"role": "user", "content": content_messages})

            response = self.openai_client.chat.completions.create(
                model="gpt-4o",
                messages=messages,
                temperature=0.1,
                max_tokens=200,
                frequency_penalty=0,
                presence_penalty=0
            )
            result = response.choices[0].message.content
        except OpenAIError as e:
            raise CustomHTTPException(
                status_code=e.__getattribute__('status_code'),
                detail=str(e.__dict__['body']['message']),
                error_code=ErrorCode.LLM_ERROR.value
            )
        except Exception as e:
            raise e

        return result
