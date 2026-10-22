import json

from dir_config import UPDATES_DATA_FILE


def get_update_data() -> dict:
    with open(UPDATES_DATA_FILE, "r", encoding="utf-8") as file:
        return json.load(file)