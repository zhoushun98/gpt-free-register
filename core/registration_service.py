# -*- coding: utf-8 -*-
"""
注册任务服务层：
    - 线程池并发执行 run_registration
    - 每个任务在 data/registration_jobs.json 里有一条记录
    - 每个任务的日志写到 data/logs/<job_uuid>.log，便于 Web UI 实时尾巴

使用：
    submit_registration(email_source="outlook", count=5)
    → 创建 5 个任务，丢入线程池，立即返回 [job_dict, ...]
"""
import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path
from typing import Any

from core import db

logger = logging.getLogger(__name__)

# 全局线程池，最大并发数（可在 web 启动时调整）
_DEFAULT_MAX_WORKERS = 4
_executor: ThreadPoolExecutor | None = None
_executor_lock = threading.Lock()


def _random_display_name() -> str:
    """生成符合 OpenAI 限制的英文字母显示名。"""
    import random
    import string

    first = random.choice(string.ascii_uppercase) + "".join(
        random.choices(string.ascii_lowercase, k=random.randint(3, 6))
    )
    last = random.choice(string.ascii_uppercase) + "".join(
        random.choices(string.ascii_lowercase, k=random.randint(3, 6))
    )
    return f"{first} {last}"


def _prepare_registration_args() -> tuple[str, str, str]:
    """复用 CLI 的默认规则，为旧 Web 任务入口补齐注册参数。"""
    # 用模块属性读，支持 WebUI 热加载
    from config import register as _r, email as _e
    from core.email_provider import acquire_email

    email = _r.REGISTER_EMAIL
    name = _r.REGISTER_NAME

    if not email:
        if _e.USE_EMAIL_SERVICE:
            email = acquire_email()
        else:
            raise RuntimeError("Web 任务入口无法交互输入邮箱，请在 config.REGISTER_EMAIL 配置邮箱")

    if not name:
        if _e.USE_EMAIL_SERVICE:
            name = _random_display_name()
        else:
            raise RuntimeError("Web 任务入口无法交互输入名称，请在 config.REGISTER_NAME 配置显示名")

    return email, name, _r.REGISTER_BIRTHDAY


def get_executor(max_workers: int | None = None) -> ThreadPoolExecutor:
    global _executor
    with _executor_lock:
        if _executor is None:
            _executor = ThreadPoolExecutor(
                max_workers=max_workers or _DEFAULT_MAX_WORKERS,
                thread_name_prefix="reg-worker",
            )
    return _executor


def shutdown_executor(wait: bool = True) -> None:
    global _executor
    with _executor_lock:
        if _executor is not None:
            _executor.shutdown(wait=wait, cancel_futures=False)
            _executor = None


# ============================================================
# 单任务执行：日志重定向到任务专属文件
# ============================================================

class _JobLogContext:
    """让本线程的根 logger 多一个 FileHandler，结束后移除。"""

    def __init__(self, log_path: str):
        self.log_path = log_path
        self.handler: logging.FileHandler | None = None

    def __enter__(self):
        Path(self.log_path).parent.mkdir(parents=True, exist_ok=True)
        self.handler = logging.FileHandler(self.log_path, encoding="utf-8")
        self.handler.setLevel(logging.INFO)
        self.handler.setFormatter(logging.Formatter(
            "%(asctime)s [%(levelname)s] [%(threadName)s] %(message)s",
            datefmt="%H:%M:%S",
        ))
        # 仅给本线程过滤 —— 用 thread name 做区分，避免污染其他任务的日志
        thread_name = threading.current_thread().name
        self.handler.addFilter(lambda r: r.threadName == thread_name)
        logging.getLogger().addHandler(self.handler)
        return self

    def __exit__(self, exc_type, exc, tb):
        if self.handler is not None:
            self.handler.close()
            logging.getLogger().removeHandler(self.handler)


def _run_one_job(job_id: int, log_file: str) -> None:
    """单任务入口（线程池里跑这个）。"""
    log_logger = logging.getLogger(__name__)

    # 取消检查：用户可能在任务排队期间点了"取消排队"，把 status 改成了 cancelled。
    # 因为 Future 已经 submit 进线程池无法撤回，只能在真正执行前自检一下，跳过 cancelled 的。
    current = db.get_job(job_id)
    if current and current.get("status") == "cancelled":
        log_logger.info(f"[Job {job_id}] 已被用户取消，跳过执行")
        return

    db.update_job(job_id, status="running", started_at=datetime.now().isoformat(timespec="seconds"))

    try:
        with _JobLogContext(log_file):
            from main import run_registration
            log_logger.info(f"[Job {job_id}] 开始注册任务")
            email, name, birthday = _prepare_registration_args()
            db.update_job(job_id, email=email)
            result = run_registration(email=email, name=name, birthday=birthday)
            if isinstance(result, dict) and result.get("success"):
                db.update_job(
                    job_id,
                    status="success",
                    email=result.get("email"),
                    account_id=result.get("account_id"),
                    completed_at=datetime.now().isoformat(timespec="seconds"),
                )
                log_logger.info(f"[Job {job_id}] 成功: {result.get('email')}")
            else:
                # 注意：失败也可能伴随 account_id（如 Codex 失败但账号已注册成功）
                err = (result or {}).get("error") if isinstance(result, dict) else "unknown"
                db.update_job(
                    job_id,
                    status="failed",
                    email=(result or {}).get("email") if isinstance(result, dict) else None,
                    account_id=(result or {}).get("account_id") if isinstance(result, dict) else None,
                    error=str(err)[:500],
                    completed_at=datetime.now().isoformat(timespec="seconds"),
                )
                log_logger.error(f"[Job {job_id}] 失败: {err}")
    except Exception as exc:
        log_logger.exception(f"[Job {job_id}] 异常")
        db.update_job(
            job_id,
            status="failed",
            error=f"{type(exc).__name__}: {exc}"[:500],
            completed_at=datetime.now().isoformat(timespec="seconds"),
        )


# ============================================================
# 公共接口
# ============================================================

def submit_registration(count: int = 1, email_source: str | None = None) -> list[dict]:
    """
    创建 N 个注册任务并提交到线程池。
    email_source 仅记录到 DB；实际邮箱来源固定为 Outlook 账号池。

    Returns:
        N 个新创建的 job dict
    """
    if email_source is None:
        from config import EMAIL_SOURCE
        email_source = EMAIL_SOURCE

    executor = get_executor()
    jobs = []
    for _ in range(count):
        job = db.create_job(email_source=email_source)
        jobs.append(job)
        executor.submit(_run_one_job, job["id"], job["log_file"])
        # 微小错峰，避免完全同步打 sentinel
        time.sleep(0.1)
    logger.info(f"[Service] 已提交 {count} 个注册任务，源={email_source}")
    return jobs


def cancel_pending_jobs() -> int:
    """
    把所有 status=pending 的任务批量改成 cancelled，避免它们被执行。
    已经在 running 的任务不动（线程池中无法中途打断）。
    返回成功取消的数量。

    实际"不执行"的保证在 _run_one_job 开头——它真要跑起来时会先看 status 决定是否跳过。
    """
    jobs = db.list_jobs(limit=1000)
    cancelled = 0
    now_iso = datetime.now().isoformat(timespec="seconds")
    for job in jobs:
        if job.get("status") == "pending":
            db.update_job(
                int(job["id"]),
                status="cancelled",
                completed_at=now_iso,
                error="用户手动取消",
            )
            cancelled += 1
    logger.info(f"[Service] 已取消 {cancelled} 个排队任务")
    return cancelled


def read_job_log(job_id: int, max_bytes: int = 50_000) -> str:
    """读取任务日志文件最后 max_bytes 字节，给 Web UI 显示。"""
    job = db.get_job(job_id)
    if not job or not job.get("log_file"):
        return ""
    p = Path(job["log_file"])
    if not p.exists():
        return ""
    size = p.stat().st_size
    with p.open("rb") as f:
        if size > max_bytes:
            f.seek(size - max_bytes)
        data = f.read()
    return data.decode("utf-8", errors="replace")
