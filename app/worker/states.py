"""逐行 / 任务状态常量与中文说明。"""

# 行状态（持久化的稳定状态，不保存易失的“处理中”状态，保证崩溃可安全重入）
ROW_PENDING = "PENDING"                    # 待处理
ROW_SUBMITTED_UNCONFIRMED = "UNCONFIRMED"  # 已开始提交但未确认（只能查，禁止自动重提）
ROW_CONFIRMED = "CONFIRMED"                # 已确认存在且岩性一致（含新填报与查询命中）
ROW_SKIPPED = "SKIPPED"                    # 查询命中唯一且岩性相同，跳过
ROW_CONFLICT = "CONFLICT"                  # 多条 / 岩性不符，停行待核实
ROW_ERROR = "ERROR"                        # 浏览器或门户运行时错误，重试耗尽

ROW_STATUS_TEXT = {
    ROW_PENDING: "待处理",
    ROW_SUBMITTED_UNCONFIRMED: "已提交未确认（仅可续查，禁止自动重提）",
    ROW_CONFIRMED: "已确认",
    ROW_SKIPPED: "已存在且一致，跳过",
    ROW_CONFLICT: "内容冲突，停行待核实",
    ROW_ERROR: "运行时错误，待重试",
}

# 终态
ROW_TERMINAL = (ROW_CONFIRMED, ROW_SKIPPED)
# 续查/恢复时需要处理的状态
ROW_DUE = (ROW_PENDING, ROW_SUBMITTED_UNCONFIRMED, ROW_CONFLICT, ROW_ERROR)

# 任务状态
TASK_QUEUED = "QUEUED"
TASK_RUNNING = "RUNNING"
TASK_WAITING = "WAITING_VERIFICATION"  # 停在待核实行 / 未确认行
TASK_COMPLETED = "COMPLETED"
TASK_FAILED = "FAILED"

TASK_STATUS_TEXT = {
    TASK_QUEUED: "排队中",
    TASK_RUNNING: "处理中",
    TASK_WAITING: "停行待核实/待续查",
    TASK_COMPLETED: "全部完成",
    TASK_FAILED: "运行时错误中止",
}
