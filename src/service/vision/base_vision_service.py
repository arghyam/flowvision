from abc import ABC, abstractmethod


class BaseVisionService(ABC):
    @abstractmethod
    def extract(image=None, image_bytes: bytes | None = None, download_url: str | None = None):
        pass

    def system_context(self):
        context ="""
        You are an expert tasked with extracting meter readings from images of bulk flow meters. The meter reading is displayed in a numeric format and indicates the total volume measured by the meter. The images provided will be clear and focused on the meter display. When processing each image, follow these steps:
        1. Extract the meter reading. Read all the digits before the m³ text. All meter readings will have a minimum of 5 digits.
        2. If the last number is black, then no decimal before last number. If last number is red, then add a decimal point before the last number.
        3. If the last digit is in between two numbers, choose the lower number of the two.
        4. Ensure accuracy by double-checking the numbers for clarity
        5. Return only the meter reading in the response.
        Analyze the following image of a water meter and provide the exact reading value displayed on the meter based on the following rules
        - If the provided image has a water meter, please respond with the exact meter reading.
        - If the provided image is not of a water meter, return an error message \\"nometer\\".
        - If the image is unclear or there are issues preventing an accurate reading, reply with the message \\"unclear\\".
        Here are a few examples of the meter reading responses - 0009873, 002353.4, 16040
        """
        return context
