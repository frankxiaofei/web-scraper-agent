"""MongoDB 存储层（向后兼容，实现见 mongo_repository.py）。"""

from src.db.mongo_repository import (
    MongoNoticeRepository,
    MongoRepository,
    create_mongo_repository,
)

__all__ = ["MongoNoticeRepository", "MongoRepository", "create_mongo_repository"]
