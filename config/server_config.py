import os
import socket
from helpers.logger import setup_logging
setup_logging()
from fastapi import FastAPI
from pathlib import Path

from config.app_info import APP_NAME, APP_VERSION, DESCRIPTION

DEV_ENV = False

LOCAL_PROTOCOL = "http"
LOCAL_IP = socket.gethostbyname(socket.gethostname())
LOCAL_PORT = 8000

FINAL_IP = f"{LOCAL_PROTOCOL}://{LOCAL_IP}:{LOCAL_PORT}"

PROD_PROTOCOL = "https"
PROD_SUBDOMAIN = "downflow"
PROD_DOMAIN = "onrender.com"

PROD_URL = f"{PROD_PROTOCOL}://{PROD_SUBDOMAIN}.{PROD_DOMAIN}"

SERVER_URL = FINAL_IP if DEV_ENV else PROD_URL

app = FastAPI(
    title=APP_NAME,
    description=DESCRIPTION,
    version=APP_VERSION,
    docs_url=None, redoc_url=None, openapi_url=None,   # served by app.py behind the login
)

SERVER_HOST = "0.0.0.0"
SERVER_PORT = int(os.environ.get("PORT") or 8000)   # Render/Heroku-style hosts inject PORT
