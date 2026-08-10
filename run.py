"""Dev/production entry: python run.py  (single worker by design — SQLite)."""
import uvicorn

from salesagent.settings import load_settings

if __name__ == "__main__":
    settings = load_settings()
    uvicorn.run("salesagent.web.app:app", host="0.0.0.0", port=settings.port,
                workers=1)
