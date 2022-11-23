from pathlib import Path
from motor.motor_asyncio import AsyncIOMotorClient

db = AsyncIOMotorClient(Path('.mongodb').read_text().strip())
