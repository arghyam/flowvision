# Flow Vision

1. Clone repository and go to the root directory.
2. Use `conda` or `python3 -m venv venv` to create a virtual environment. Activate the virtual environment.
3. Run `pip install -r requirements.txt` to install all the required dependencies.
4. Run `python src/run.py` to start ASGI server.
5. The following are the endpoints exposed from the service
    (a) POST /flowvision/v1/extract-reading
    (b) POST /flowvision/v1/feedback
6. The [API specification](flowvision_api_spec.yaml) will give more details about the request and response structure

## Use Qwen2-VL model

- Choose the `Qwen/Qwen2-VL-2B-Instruct` model in the conf/config.yaml file.

## Use OpenAI model

- Choose the `gpt-4o` model in the conf/config.yaml file.