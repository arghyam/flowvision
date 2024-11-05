import logging
from http import HTTPStatus
import traceback
from datetime import datetime
from fastapi import HTTPException

from validation.validators import ImageValidator
from service.llm_services.openai_llm_service import OpenAILLMService
from models.api_models import RequestForm, Response, Error, DataField


class APIService:
    def __init__(self):
        self.llm_service = OpenAILLMService()
        self.image_validator = ImageValidator()

    def construct_response(self, id: str, response_code: str, status_code: int, data: DataField | Error, params=None):
        return Response(
            id=id,
            ts=datetime.now().isoformat(),
            responseCode=response_code,
            statusCode=status_code,
            data=data,
            params=params
        )

    def _send_to_storage(self, image_bytes):
        pass

    async def read_meter(self, request: RequestForm) -> Response:
        status_code = HTTPStatus.OK.value
        response_code = HTTPStatus.OK.phrase

        try:
            image_bytes = await self.image_validator.validate(request)
            self._send_to_storage(image_bytes=image_bytes)
            answer = self.llm_service.read(image_bytes)
            await request.data.close()

            data = DataField(data=answer)

            return self.construct_response(
                id=request.id,
                response_code=response_code,
                status_code=status_code,
                data=data,
                params=None
            )

        except HTTPException as e:
            status_code = e.status_code
            response_code = e.detail['phrase']
            error_message = e.detail['phrase']
            error_details = e.detail['details']
            logging.error("\nError type: %s\nTrace: %s", error_message, traceback.format_exc())

            data = Error(
                errorCode=status_code,
                errorMessage=error_message,
                errorDetails=error_details
            )

            return self.construct_response(
                id=request.id,
                response_code=response_code,
                status_code=status_code,
                data=data,
                params=None
            )

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

            return self.construct_response(
                id=request.id,
                response_code=response_code,
                status_code=status_code,
                data=data,
                params=None
            )

        finally:
            if request.data:
                await request.data.close()
