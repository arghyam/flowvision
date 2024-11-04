import logging
from http import HTTPStatus
import traceback
from datetime import datetime

from service.llm_services.openai_llm_service import OpenAILLMService
from models.api_models import RequestForm, Response, Error, DataField


class APIService:
    def __init__(self):
        self.llm_service = OpenAILLMService()

    def _send_to_storage(self, image_bytes):
        pass

    async def read_meter(self, request: RequestForm) -> Response:
        status_code = HTTPStatus.OK.value
        response_code = HTTPStatus.OK.phrase

        try:
            image_bytes = await request.data.read()
            self._send_to_storage(image_bytes=image_bytes)
            answer = self.llm_service.read(image_bytes)
            data = DataField(data=answer)

        except Exception as e:
            status_code = HTTPStatus.INTERNAL_SERVER_ERROR.value
            response_code = HTTPStatus.INTERNAL_SERVER_ERROR.phrase

            error_message = type(e).__name__
            error_details = str(e)
            logging.error("\nError type: %s\nTrace: %s", error_message, traceback.format_exc())

            data = Error(
                errorCode=HTTPStatus.INTERNAL_SERVER_ERROR.value,
                errorMessage=error_message,
                errorDetails=error_details
            )
        finally:
            return Response(
                id=request.id,
                ts=datetime.now().isoformat(),
                responseCode=response_code,
                statusCode=status_code,
                data=data,
                params=None
            )
