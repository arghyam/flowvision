from dotenv import load_dotenv

from app_factory import create_app

load_dotenv()
app = create_app()
