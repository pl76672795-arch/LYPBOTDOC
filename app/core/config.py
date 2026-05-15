import os

# Cargar variables locales si existe un archivo .env
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

class Config:
    telegram_token: str = os.getenv("TELEGRAM_TOKEN")
    gemini_api_key: str = os.getenv("GEMINI_API_KEY")
    deepseek_api_key: str = os.getenv("DEEPSEEK_API_KEY")
    jsonpe_token: str = os.getenv("JSONPE_TOKEN")

config = Config()

if not config.telegram_token:
    raise ValueError("TELEGRAM_TOKEN no está configurado.")