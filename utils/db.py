from pymongo import MongoClient
import os
def get_mongo():
    uri = os.environ.get('MONGODB_URI') or os.environ.get('MONGO_URI')
    if not uri:
        return None
    try:
        client = MongoClient(uri, serverSelectionTimeoutMS=5000)
        client.admin.command('ping')
        return client
    except Exception as e:
        print('Mongo connection failed:', e)
        return None
