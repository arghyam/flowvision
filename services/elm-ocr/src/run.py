import uvicorn

from conf.config import Config

if __name__ == "__main__":

    config = Config()

    app = config.find("app_server.app", "routes:app")
    port = config.find("app_server.port", 8000)
    log_level = config.find("log_level", "info")

    print("Starting server")
    uvicorn.run(app=app, host="0.0.0.0", port=port, log_level=log_level, timeout_keep_alive=60)
