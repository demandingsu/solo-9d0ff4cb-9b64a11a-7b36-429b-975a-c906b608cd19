"""逐行 / 任务状态常量与中文说明。"""

# 行状态
# PENDING 与 SUBMITTING 是关键分界：
# - PENDING/ERROR：从未点击过保存（打开录入页或填字段就失败也算），可安全重试填报；
# - SUBMITTING：已持久化“保存动作开始”，点击可能已经发出，崩溃/超时后也禁止重提；
# - UNCONFIRMED：已点击保存但未拿到确认（超时/断线/延迟可见），只查不填。
ROW_PENDING = "PENDING"                    # 待处理（从未开始保存，可重试）
ROW_SUBMITTING = "SUBMITTING"              # 保存已开始（含点击前的临界窗口），无法排除已提交
ROW_SUBMITTED_UNCONFIRMED = "UNCONFIRMED"  # 已开始提交但未确认（只能查，禁止自动重提）
ROW_CONFIRMED = "CONFIRMED"                # 已确认存在且岩性一致（含新填报与查询命中）
ROW_SKIPPED = "SKIPPED"                    # 查询命中唯一且岩性相同，跳过
ROW_CONFLICT = "CONFLICT"                  # 多条 / 岩性不符，停行待核实
ROW_ERROR = "ERROR"                        # 点击保存之前的运行时错误，重试耗尽，仍可重试

ROW_STATUS_TEXT = {
    ROW_PENDING: "待处理（尚未开始保存，可重试）",
    ROW_SUBMITTING: "保存已开始/无法排除已提交（只查不填，禁止重提）",
    ROW_SUBMITTED_UNCONFIRMED: "已提交未确认（仅可续查，禁止自动重提）",
    ROW_CONFIRMED: "已确认",
    ROW_SKIPPED: "已存在且一致，跳过",
    ROW_CONFLICT: "内容冲突，停行待核实",
    ROW_ERROR: "运行时错误（尚未保存，可重试）",
}

# 终态：任何场景下都不再动作
ROW_TERMINAL = (ROW_CONFIRMED, ROW_SKIPPED)
# 已开始或无法排除已开始保存：超时/查询报错/重复上传/重启后始终只查不填
ROW_RECHECK_ONLY = (ROW_SUBMITTING, ROW_SUBMITTED_UNCONFIRMED)
# 续查/恢复时需要推进的状态
ROW_DUE = (ROW_PENDING, ROW_SUBMITTING, ROW_SUBMITTED_UNCONFIRMED,
           ROW_CONFLICT, ROW_ERROR)

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
