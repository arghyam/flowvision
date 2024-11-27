from abc import ABC, abstractmethod


class BaseVisionService(ABC):
    @abstractmethod
    def extract(image=None, image_bytes: bytes | None = None, download_url: str | None = None):
        pass

    def system_context(self):
        context = """
        You are an AI model tasked with extracting meter readings from images of bulk flow meters. The meter reading is displayed in a numeric format and indicates the total volume measured by the meter. The images provided will be clear and focused on the meter display. When processing each image, follow these steps:
        1. Identify the numeric display on the meter and extract the numeric value
        2. Retain the leading zeros if applicable
        3. If the last digit from the right is of red colour, retain the last digit as a decimal point. Else, extract the whole number.
        4. Ensure accuracy by double-checking the numbers for clarity
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
