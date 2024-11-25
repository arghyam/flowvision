import os
import operator
import yaml

from functools import reduce


class Config:
    def __init__(self):
        load_dotenv()
        default_config_file_path = "src/conf/config.yaml"
        config_file = os.getenv("CONFIG_PATH") or default_config_file_path
        with open(config_file, "r") as f:
            self.config = yaml.safe_load(f)

    def find(self, path: str, default=None):
        try:
            if self.config:
                element_value = reduce(operator.getitem, path.split("."), self.config)
            else:
                raise KeyError
        except KeyError:
            element_value = None
        finally:
            return element_value or default
