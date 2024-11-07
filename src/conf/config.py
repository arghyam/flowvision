import os
import logging
import operator
import yaml

from dotenv import load_dotenv
from functools import reduce


class Config:
    def __init__(self):
        load_dotenv()
        self.__openai_api_key = os.getenv("OPENAI_API_KEY")

        config_file = os.getenv("CONFIG_PATH")
        with open(config_file, "r") as f:
            self.config = yaml.safe_load(f)

    def openai_api_key(self):
        return self.__openai_api_key

    def find(self, path, default=None):
        try:
            if self.config:
                element_value = reduce(operator.getitem, path.split("."), self.config)
            else:
                raise KeyError
        except KeyError:
            # element_value = None
            logging.warning(f"Path '{path}' not found in config file.")
        finally:
            return element_value or default
