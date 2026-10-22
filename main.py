import logging
import os
import uvicorn
from config.server_config import app, SERVER_HOST, SERVER_PORT, FINAL_IP, PROD_URL
from config.app_info import APP_NAME
import app as routes


if __name__ == "__main__":
    logging.getLogger("downflow").info("%s running at %s  (public: %s)", APP_NAME, FINAL_IP, PROD_URL)

    uvicorn.run(
        "main:app",
        host=SERVER_HOST,
        port=SERVER_PORT,
        reload=os.environ.get("DOWNFLOW_RELOAD", "0") == "1",
        log_config=None
    )
