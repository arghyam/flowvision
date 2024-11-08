from openai import OpenAI
import base64

from service.vision.base_vision_service import BaseVisionService
from conf.config import Config


class OpenAIVisionService(BaseVisionService):
    def __init__(self):
        config = Config()
        self.openai_client = OpenAI(api_key=config.openai_api_key())

    def system_context(self):
        context = """
        You are an AI model tasked with extracting meter readings from images of bulk flow meters. The meter reading is displayed in a numeric format and indicates the total volume measured by the meter. The images provided will be clear and focused on the meter display. When processing each image, follow these steps:
        1. Identify the numeric display on the meter
        2. Extract the numeric value shown along with the leading zeros.
        3. Ensure accuracy by double-checking the numbers for clarity.
        Analyze the following image of a water meter and provide the exact reading value displayed on the meter based on the following rules
        - If the image is unclear or there are issues preventing an accurate reading, reply with the message \\"unclear\\".
        - If the provided image is not of a water meter, return an error message \\"nometer\\".
        - If the provided image has a water meter, please respond with the exact meter reading.
        Here are a few examples of the meter reading responses:
        1. 0035965
        2. 004583
        3. 16040
        """
        return context

    # Will need to change to just use url
    def read(self, image_bytes):
        base64_image = base64.b64encode(image_bytes).decode('utf-8')
        messages = []
        messages.append({"role": "system", "content": [{"type": "text", "text": self.system_context()}]})
        messages.append({"role": "user", "content": [
            {"type": "text", "text": "Extract the meter readings from the image"},
            {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{base64_image}"}}
        ]})

        response = self.openai_client.chat.completions.create(
            model="gpt-4o",
            messages=messages,
            temperature=0.1,
            max_tokens=200,
            frequency_penalty=0,
            presence_penalty=0
        )
        result = response.choices[0].message.content
        return result
