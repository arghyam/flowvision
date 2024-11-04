from fastapi import FastAPI, Form
from typing_extensions import Annotated

# from api_services.api_service import APIService
from service.api_services import APIService
from conf.config import Config
from models.api_models import RequestForm

app = FastAPI()
api_service = APIService()
config = Config()


@app.get("/")
async def root():
    return {"message": "Hi, I am the meter reading assistant."}


ai_tool_endpoint = "/ai-tools/v1"


@app.post(f"{ai_tool_endpoint}/read_meter")
async def read_meter(request: Annotated[RequestForm, Form()]):
    return await api_service.read_meter(request)


if __name__ == "__main__":
    import uvicorn
    app = config.find("uvicorn_server.app")
    port = config.find("uvicorn_server.port")
    log_level = config.find("uvicorn_server.log_level")

    if app is None:
        raise KeyError("Path to uvicorn server app not found in config file.")
    if port is None:
        raise KeyError("Path to uvicorn server port not found in config file.")

    print("=" * 100)
    print("Starting server")
    config = uvicorn.Config(app=app, port=port, log_level=log_level)
    server = uvicorn.Server(config)
    server.run()
