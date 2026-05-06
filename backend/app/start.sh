#!/bin/bash

echo "=== 应用启动脚本 ==="

# 等待数据库就绪
echo "等待数据库连接..."
python -c "
import time
import os
import psycopg2
from psycopg2 import OperationalError

max_retries = 30
retry_count = 0

while retry_count < max_retries:
    try:
        conn = psycopg2.connect(os.environ['DATABASE_URL'])
        conn.close()
        print('数据库连接成功!')
        break
    except OperationalError:
        retry_count += 1
        print(f'等待数据库... ({retry_count}/{max_retries})')
        time.sleep(2)
else:
    print('数据库连接失败!')
    exit(1)
"

# 检查并运行数据库迁移
echo "检查数据库迁移状态..."
# 简单地运行 upgrade head 即可，Alembic 会自动处理首次创建和后续升级
alembic upgrade head

echo "启动应用服务..."
exec "$@" 