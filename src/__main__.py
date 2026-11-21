# python -m src   (host and port come from config.py)
import uvicorn

from .config import get_settings

if __name__ == "__main__":
    settings = get_settings()
    uvicorn.run("src.main:app", host=settings.host, port=settings.port)
