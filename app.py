"""Entry point.  Production: gunicorn -c gunicorn.conf.py app:app   Local: python app.py"""
import os

from ifcgeoref import create_app

# Worker processes re-import this module as "__mp_main__"; they must not start a second app.
app = create_app() if __name__ in ("__main__", "app") else None

if __name__ == "__main__":
    app.run(
        host=os.getenv("HOST", "127.0.0.1"),
        port=int(os.getenv("PORT", "5002")),
        debug=os.getenv("FLASK_DEBUG", "0") == "1",
        use_reloader=False,
    )
