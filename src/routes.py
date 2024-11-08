from fastapi import FastAPI, Form
from typing_extensions import Annotated

from service.api import ImageService
from conf.config import Config
from models.models import ImageUploadRequest, ReadingExtractionRequest

app = FastAPI()
config = Config()
flow_vision_service = ImageService(config=config)
basepath = "/api/v1/flowvision"


@app.get("/")
async def root():
    return {"message": "Hi, I am the meter reading assistant."}


@app.post(f"{basepath}/uploadImage")
async def upload_image(request: Annotated[ImageUploadRequest, Form()]):
    return await flow_vision_service.upload_image(request)


@app.post(f"{basepath}/extractReading")
async def extract_reading(request: ReadingExtractionRequest):
    return await flow_vision_service.extract_reading(request)


if __name__ == "__main__":
    import uvicorn
    app = config.find("app_server.app", "routes:app")
    port = config.find("app_server.port", 8000)
    log_level = config.find("log_level", "info")

    # if app is None:
    #     raise KeyError("Path to application server app not found.")
    # if port is None:
    #     raise KeyError("Path to uvicorn server port not found in config file.")

    print("Starting server")
    config = uvicorn.Config(app=app, port=port, log_level=log_level)
    server = uvicorn.Server(config)
    server.run()
