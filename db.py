import os
from pymongo import MongoClient

MONGO_URI = os.environ.get("MONGO_URI", "mongodb://localhost:27017")

_mongo = MongoClient(MONGO_URI)
_db_name = os.environ.get("MONGO_DB_NAME", "realty_line")
db = _mongo.get_database(_db_name)

col_users      = db["users"]
col_api_keys   = db["api_keys"]
col_api_usage  = db["api_usage"]
col_line_cache = db["line_cache"]
col_messages   = db["messages"]
col_schedules  = db["schedules"]

try:
    col_api_keys.create_index([("key_hash", 1)], unique=True)
    col_api_keys.create_index([("user_id", 1), ("created_at", -1)])
    col_api_usage.create_index([("user_id", 1), ("ts", -1)])
    col_api_usage.create_index([("key_id", 1), ("ts", -1)])
    col_api_usage.create_index("ts", expireAfterSeconds=60 * 60 * 24 * 90)
    col_messages.create_index([("user_id", 1), ("id", 1)], unique=True)
    col_messages.create_index([("user_id", 1), ("peer", 1), ("created_time", -1)])
except Exception:
    pass
