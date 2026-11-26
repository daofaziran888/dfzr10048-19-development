"""业务路由汇总：所有模块共用一套资源路由，由领域服务完成分派。"""
from __future__ import annotations

from app.routers import redeliver_stage, resource

# 领办路由含 /api/fail_redeliver/todos 等固定路径，必须先于通用资源路由注册。
ROUTERS = [redeliver_stage, resource]
