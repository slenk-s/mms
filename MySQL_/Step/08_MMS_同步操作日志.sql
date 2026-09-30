-- MySQL 同步幂等操作日志迁移
-- 业务写入和本表写入由客户端在同一事务中提交。

CREATE TABLE IF NOT EXISTS `MMS_SyncOperationLog` (
    `operation_id` VARCHAR(64) NOT NULL COMMENT '本地同步操作 UUID',
    `table_name` VARCHAR(128) NOT NULL COMMENT '业务表名',
    `operation` VARCHAR(16) NOT NULL COMMENT 'INSERT/UPDATE/DELETE',
    `record_id` VARCHAR(128) NOT NULL COMMENT '业务记录 ID',
    `processed_at` DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '处理时间',
    `result` VARCHAR(32) NOT NULL COMMENT '处理结果',
    PRIMARY KEY (`operation_id`),
    KEY `idx_sync_operation_processed_at` (`processed_at`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
  COMMENT='离线同步操作幂等日志';
