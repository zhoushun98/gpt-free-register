# -*- coding: utf-8 -*-
"""
Flask 本地控制台。

复用现有后端：
    core.db                     —— 账号 / 邮箱池 / 任务的文件持久化与查询
    core.registration_service   —— 线程池批量注册 + 任务日志
    webui.config_editor         —— 安全读写 config/*.py

所有接口返回 JSON；前端是单文件 templates/index.html（原生 JS + fetch）。
默认绑定 127.0.0.1，仅本地访问。
"""
import logging
from pathlib import Path

from flask import Flask, Response, jsonify, render_template, request

from core import db
from core import registration_service as svc
from webui import config_editor

logger = logging.getLogger(__name__)

# 正在补跑 Codex 的邮箱集合（进程内防重复触发）
_codex_retrying: set[str] = set()

_LOG_DIR = Path(__file__).resolve().parent.parent / "注册日志"


def _codex_retry_log_path(email: str) -> Path:
    safe = email.replace("/", "_").replace("\\", "_").replace(":", "_")
    return _LOG_DIR / f"codex-retry-{safe}.log"


def create_app() -> Flask:
    app = Flask(__name__, template_folder="templates")

    # ----------------------------------------------------------
    # 页面
    # ----------------------------------------------------------
    @app.get("/")
    def index():
        return render_template("index.html")

    # ----------------------------------------------------------
    # 统计概览
    # ----------------------------------------------------------
    @app.get("/api/summary")
    def api_summary():
        pool = db.outlook_pool_summary()
        return jsonify({
            "accounts": db.count_accounts(),
            "outlook_total": pool.get("total", 0),
            "outlook_available": pool.get("available", 0),
            "outlook_used": pool.get("used", 0),
            "outlook_failed": pool.get("failed", 0),
        })

    # ----------------------------------------------------------
    # 已注册账号
    # ----------------------------------------------------------
    @app.get("/api/accounts")
    def api_accounts():
        limit = request.args.get("limit", default=500, type=int)
        return jsonify(db.list_accounts(limit=limit))

    # ----------------------------------------------------------
    # 邮箱池
    # ----------------------------------------------------------
    @app.get("/api/outlook")
    def api_outlook():
        status = request.args.get("status") or None
        limit = request.args.get("limit", default=500, type=int)
        return jsonify(db.list_outlook_pool(status=status, limit=limit))

    @app.post("/api/outlook/import")
    def api_outlook_import():
        """
        粘贴文本导入邮箱素材。
        每行格式：email----password----clientId----refreshToken
        分隔符兼容 ---- 与 ====（外购素材两种都见过）。
        """
        data = request.get_json(silent=True) or {}
        text = data.get("text") or ""
        records = []
        for line in text.splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            parts = line.split("----") if "----" in line else line.split("====")
            parts = [p.strip() for p in parts]
            if len(parts) < 4:
                continue
            records.append({
                "email": parts[0],
                "password": parts[1],
                "client_id": parts[2],
                "refresh_token": parts[3],
            })
        if not records:
            return jsonify({"ok": False, "error": "未解析到有效邮箱行（需 4 段，---- 或 ==== 分隔）"}), 400
        inserted, skipped = db.import_outlook_accounts(records)
        return jsonify({"ok": True, "inserted": inserted, "skipped": skipped, "parsed": len(records)})

    @app.post("/api/outlook/status")
    def api_outlook_status():
        """手动改邮箱状态：body {email, status, note?}。status ∈ available/used/failed。"""
        data = request.get_json(silent=True) or {}
        email = (data.get("email") or "").strip()
        status = (data.get("status") or "").strip()
        if not email or status not in ("available", "used", "failed"):
            return jsonify({"ok": False, "error": "email 或 status 非法"}), 400
        db.release_outlook(email, status=status, note=data.get("note"))
        return jsonify({"ok": True})

    @app.post("/api/outlook/delete")
    def api_outlook_delete():
        """从邮箱池彻底删除一个邮箱：body {email}。"""
        data = request.get_json(silent=True) or {}
        email = (data.get("email") or "").strip()
        if not email:
            return jsonify({"ok": False, "error": "email 为空"}), 400
        deleted = db.delete_outlook(email)
        return jsonify({"ok": True, "deleted": deleted})

    # ----------------------------------------------------------
    # Codex 授权账号（CPA 兼容凭证）
    # ----------------------------------------------------------
    @app.get("/api/codex")
    def api_codex_list():
        return jsonify({
            "summary": db.codex_accounts_summary(),
            "accounts": db.list_codex_accounts(),
        })

    @app.get("/api/codex/download/<path:filename>")
    def api_codex_download(filename: str):
        """
        下载一个 CPA 兼容的 codex-*.json 文件，下载即标记为已导出（计数+1）。
        前端通过浏览器原生下载触发（a 标签 / window.location）。
        """
        try:
            content, fname = db.read_codex_credential(filename)
        except ValueError as exc:
            return jsonify({"ok": False, "error": str(exc)}), 404
        db.mark_codex_exported(fname)
        return Response(
            content,
            mimetype="application/json",
            headers={"Content-Disposition": f'attachment; filename="{fname}"'},
        )

    @app.post("/api/codex/download-bulk")
    def api_codex_download_bulk():
        """
        批量下载选中的 codex 凭证，打包到一个 JSON 文件里。

        Body: {"filenames": ["codex-xxx.json", ...]}
        响应：聚合 JSON（attachment 触发浏览器下载），结构：
            {
              "exported_at": "...",
              "count": N,
              "credentials": [{"filename": "...", "data": {...原始凭证内容...}}, ...],
              "errors": [...]   // 仅当部分失败时出现
            }
        注意：聚合格式**不能直接被 CPA 读**，CPA 是按单文件加载 auths/ 目录的。
              本接口主要用途是备份 / 跨机迁移 / 二次处理。
        每个成功的凭证会自动标记 mark_exported（计数+1）。
        """
        import json as _json
        from datetime import datetime as _dt

        data = request.get_json(silent=True) or {}
        filenames = data.get("filenames") or []
        if not isinstance(filenames, list) or not filenames:
            return jsonify({"ok": False, "error": "filenames 必须是非空数组"}), 400
        if len(filenames) > 1000:
            return jsonify({"ok": False, "error": "单次最多 1000 个"}), 400

        bundle = []
        errors = []
        for fname in filenames:
            if not isinstance(fname, str):
                errors.append({"filename": str(fname), "error": "非字符串"})
                continue
            try:
                content, real_fname = db.read_codex_credential(fname)
                parsed = _json.loads(content)
                bundle.append({"filename": real_fname, "data": parsed})
                db.mark_codex_exported(real_fname)
            except Exception as exc:
                errors.append({"filename": fname, "error": f"{type(exc).__name__}: {exc}"})

        now = _dt.now()
        result = {
            "exported_at": now.isoformat(timespec="seconds"),
            "count": len(bundle),
            "credentials": bundle,
        }
        if errors:
            result["errors"] = errors

        dl_name = f"codex-bulk-{now.strftime('%Y%m%d-%H%M%S')}.json"
        return Response(
            _json.dumps(result, ensure_ascii=False, indent=2),
            mimetype="application/json",
            headers={"Content-Disposition": f'attachment; filename="{dl_name}"'},
        )

    @app.post("/api/codex/reset-export")
    def api_codex_reset_export():
        """清掉某个 codex 凭证的导出状态（重新标为未导出）。body {filename}。"""
        data = request.get_json(silent=True) or {}
        fname = (data.get("filename") or "").strip()
        if not fname:
            return jsonify({"ok": False, "error": "filename 为空"}), 400
        try:
            db.reset_codex_exported(fname)
        except Exception as exc:
            return jsonify({"ok": False, "error": str(exc)}), 400
        return jsonify({"ok": True})

    @app.post("/api/codex/retry")
    def api_codex_retry():
        """
        手动补跑某账号的 Codex 授权。Body {email}。

        立即返回，实际跑在后台守护线程里（要 ~1-2 分钟收邮件+接码）。
        前端轮询 /api/accounts 看 codex_status 变化即可。
        防重复触发：补跑过程中再次调用同邮箱会被拒。
        """
        import threading
        data = request.get_json(silent=True) or {}
        email = (data.get("email") or "").strip()
        if not email:
            return jsonify({"ok": False, "error": "email 为空"}), 400

        # 校验账号存在
        acc = db.get_account_by_email(email)
        if acc is None:
            return jsonify({"ok": False, "error": f"账号不存在: {email}"}), 404

        # 防重复触发：内存级标记，进程内同邮箱并发 retry 直接拒
        if email in _codex_retrying:
            return jsonify({"ok": False, "error": "该账号正在补跑中，请稍候"}), 409
        _codex_retrying.add(email)

        # 立即把状态标为 retrying，前端能立刻看到
        db.update_account_codex_status(email, "retrying", None)

        log_path = _codex_retry_log_path(email)
        log_path.parent.mkdir(parents=True, exist_ok=True)
        # 清空旧日志（每次补跑重新写）
        log_path.write_text("", encoding="utf-8")

        def _bg_retry():
            import logging as _logging
            from core.codex_oauth import run_codex_oauth

            # 给本线程挂一个写到补跑日志文件的 handler
            thread_name = threading.current_thread().name
            fh = _logging.FileHandler(str(log_path), encoding="utf-8")
            fh.setLevel(_logging.DEBUG)
            fh.setFormatter(_logging.Formatter(
                "%(asctime)s [%(levelname)s] %(message)s",
                datefmt="%H:%M:%S",
            ))
            fh.addFilter(lambda r: r.threadName == thread_name)
            _logging.getLogger().addHandler(fh)
            try:
                result = run_codex_oauth(email)
                result_status = result.get("status", "failed")
                if result.get("ok"):
                    db.update_account_codex_status(email, "success", None)
                    logger.info(f"[Codex 补跑] {email} 成功")
                elif result_status == "deactivated":
                    db.update_account_codex_status(email, "deactivated", result.get("message"))
                    logger.warning(f"[Codex 补跑] {email} 账号已废: {result.get('message')}")
                else:
                    db.update_account_codex_status(
                        email, result_status,
                        result.get("message"),
                    )
                    logger.warning(f"[Codex 补跑] {email} 失败: {result.get('message')}")
            except Exception as exc:
                db.update_account_codex_status(email, "failed", f"{type(exc).__name__}: {exc}")
                logger.exception(f"[Codex 补跑] {email} 异常")
            finally:
                fh.close()
                _logging.getLogger().removeHandler(fh)
                _codex_retrying.discard(email)

        threading.Thread(target=_bg_retry, name=f"codex-retry-{email}", daemon=True).start()
        return jsonify({"ok": True, "message": "已在后台开始补跑，~1-2 分钟后刷新查看"})

    @app.get("/api/codex/retry-log")
    def api_codex_retry_log():
        """读取某邮箱最近一次补跑的日志。?email=xxx"""
        email = (request.args.get("email") or "").strip()
        if not email:
            return jsonify({"ok": False, "error": "email 为空"}), 400
        p = _codex_retry_log_path(email)
        if not p.exists():
            return jsonify({"ok": True, "log": "", "running": False})
        max_bytes = 50_000
        size = p.stat().st_size
        with p.open("rb") as f:
            if size > max_bytes:
                f.seek(size - max_bytes)
            content = f.read().decode("utf-8", errors="replace")
        return jsonify({
            "ok": True,
            "log": content,
            "running": email in _codex_retrying,
        })

    # ----------------------------------------------------------
    # 注册任务
    # ----------------------------------------------------------
    @app.get("/api/jobs")
    def api_jobs():
        limit = request.args.get("limit", default=100, type=int)
        return jsonify(db.list_jobs(limit=limit))

    @app.post("/api/jobs")
    def api_jobs_create():
        """启动批量注册：body {count, workers}。"""
        data = request.get_json(silent=True) or {}
        try:
            count = int(data.get("count", 1))
        except (TypeError, ValueError):
            return jsonify({"ok": False, "error": "count 非法"}), 400
        if count < 1 or count > 200:
            return jsonify({"ok": False, "error": "count 需在 1~200 之间"}), 400

        # workers 控制线程池并发上限（首次提交时生效）
        workers = data.get("workers")
        if workers:
            try:
                svc.get_executor(max_workers=int(workers))
            except (TypeError, ValueError):
                pass

        # 提交前先确认池里有足够可用邮箱，给前端一个温和提示（不阻断）
        pool = db.outlook_pool_summary()
        jobs = svc.submit_registration(count=count)
        warning = ""
        if pool.get("available", 0) < count:
            warning = f"可用邮箱仅 {pool.get('available', 0)} 个，少于任务数 {count}，不足的会失败"
        return jsonify({"ok": True, "submitted": len(jobs), "jobs": jobs, "warning": warning})

    @app.post("/api/jobs/cancel-pending")
    def api_jobs_cancel_pending():
        """取消所有还在排队（status=pending）的任务。已在 running 的不动。"""
        cancelled = svc.cancel_pending_jobs()
        return jsonify({"ok": True, "cancelled": cancelled})

    @app.get("/api/jobs/<int:job_id>/log")
    def api_job_log(job_id: int):
        job = db.get_job(job_id)
        if not job:
            return jsonify({"ok": False, "error": "任务不存在"}), 404
        return jsonify({
            "ok": True,
            "job": job,
            "log": svc.read_job_log(job_id),
        })

    # ----------------------------------------------------------
    # 配置读写
    # ----------------------------------------------------------
    @app.get("/api/config")
    def api_config_get():
        return jsonify(config_editor.get_config())

    @app.post("/api/config")
    def api_config_set():
        data = request.get_json(silent=True) or {}
        updates = data.get("updates") if isinstance(data.get("updates"), dict) else data
        if not isinstance(updates, dict) or not updates:
            return jsonify({"ok": False, "error": "无更新内容"}), 400
        try:
            result = config_editor.update_config(updates)
        except Exception as exc:
            logger.exception("配置写入失败")
            return jsonify({"ok": False, "error": f"{type(exc).__name__}: {exc}"}), 500

        # 写盘成功后立即热加载所有 config 子模块，让运行时代码看到新值。
        reload_ok = True
        reload_err = ""
        try:
            import config as _config_pkg
            _config_pkg.reload_all()
        except Exception as exc:
            reload_ok = False
            reload_err = f"{type(exc).__name__}: {exc}"
            logger.exception("配置热加载失败")

        return jsonify({
            "ok": True,
            "updated": result["updated"],
            "ignored": result["ignored"],
            "reloaded": reload_ok,
            "note": (
                "✅ 已保存并热加载，新值立即生效"
                if reload_ok
                else f"⚠️ 已写入文件但热加载失败（{reload_err}），需重启 Web 服务才能生效"
            ),
        })

    return app
