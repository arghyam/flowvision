from openai import OpenAI, OpenAIError
import base64

from error.error import CustomHTTPException, ErrorCode
from service.vision.base_vision_service import BaseVisionService
from conf.config import Config

import os
from PIL import Image, ImageFile

class OpenAIVisionService(BaseVisionService):
    def __init__(self):
        print("Creating OpenAI client...")

        self.openai_client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))

    def system_context(self):
        context = """
            You are an AI model tasked with extracting meter readings from images of bulk flow meters. The meter reading is displayed in a numeric format and indicates the total volume measured by the meter. The images provided will be clear and focused on the meter display. When processing each image, follow these steps:
            1. Identify the numeric display on the meter
            2. If the image is rotated, extract the meter readings by obtaining the right orientation
            3. Extract the numeric value shown along with the leading zeros
            4. If the last digit is of red colour, include the digit as a decimal point
            5. Ensure accuracy by double-checking the numbers for clarity
            6. Do not hallucinate.
            Analyze the following image of a water meter and provide the exact reading value displayed on the meter based on the following rules
            - If the provided image has a water meter, please respond with the exact meter reading.
            - If the provided image is not of a water meter, return an error message \\"nometer\\".
            - If the image is unclear or there are issues preventing an accurate reading, reply with the message \\"unclear\\".
            Here are a few examples of the meter reading responses:
            1. 0009873
            2. 002353.4
            3. 16040
            """
        return context


    # Will need to change to just use url
    def extract(self, image = None, image_bytes: bytes | None = None, download_url: str | None = None) -> str:
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
