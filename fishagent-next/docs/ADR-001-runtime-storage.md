# ADR-001: 运行时存储职责

## 状态

Accepted

## 决策

- PostgreSQL 保存业务事实、关系投影和 Outbox。
- Redis 仅用于 Celery 队列、结果和实时发布，不作为最终状态源。
- MinIO 保存图片、视频帧和证据对象，数据库只保存对象元数据和引用。
- SQLite 只作为个人开发或单进程测试的可选替代，不作为 Compose 生产路径。

## 原因

系统同时运行 Web、MQTT、Beat、普通 Worker 和视觉 Worker。它需要跨进程事务、唯一幂等约束、行级并发控制、任务恢复和事件序列。SQLite 的文件锁和单文件边界不适合作为共享生产事实源；把二进制帧放入关系数据库也会增加备份、查询和生命周期成本。

## 后果

本地启动需要 PostgreSQL、Redis、MinIO 和 MQTT。代价是基础设施更多，但开发、CI、演示和生产使用同一套并发语义。Redis 或 MinIO 暂时不可用时，健康状态会降级，遥测事实仍由 PostgreSQL 保留。
