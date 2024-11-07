from fastapi import FastAPI, Form
from typing_extensions import Annotated

# from api_services.api_service import APIService
from service.api import ImageService
from conf.config import Config
from models.models import Request

app = FastAPI()
flow_vision_service = ImageService()
config = Config()
basepath = "/api/v1/flowvision"


@app.get("/")
async def root():
    return {"message": "Hi, I am the meter reading assistant."}


@app.post(f"{basepath}/uploadImage")
async def read_meter(request: Annotated[Request, Form()]):
    return await flow_vision_service.read_meter(request)


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
