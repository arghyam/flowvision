Flow Vision

Steps to run api:
1. Clone repository and go to the root directory.
2. Set `OPENAI_API_KEY` in os env to use OpenAI's GPT-4o model.
3. Create and activate python virtual environment.
4. Run `pip install -r requirements.txt` to install dependencies.
5. Go to src directory using `cd src`.
6. Run `python3 routes.py` to start uvicorn server.
7. Use a tool like Postman to send api POST requests to this endpoint http://127.0.0.1:8000/ai-tools/v1/read_meter.
(Note that post requests must be sent as form data.)