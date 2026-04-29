#!/usr/bin/env python3
"""
天翼云登录服务
提供登录接口和前端页面
"""

from flask import Flask, Response, render_template, request, jsonify
import login_module

app = Flask(__name__)
app.template_folder = "templates"


@app.route("/")
def index():
    """首页 - 登录页面"""
    return render_template("login.html")


@app.route("/api/login", methods=["POST"])
def api_login():
    """发起登录。

    成功时直接返回原始 storage_state JSON。
    需要短信验证码时返回 202 和 session_id。
    """
    payload = request.get_json(silent=True) or {}
    phone = payload.get("phone", "").strip()
    password = payload.get("password", "").strip()

    if not phone or not password:
        return jsonify({
            "success": False,
            "message": "请输入手机号和密码"
        }), 400

    result = login_module.start_login_session(phone, password)
    return _build_login_response(result)


@app.route("/api/login/verify", methods=["POST"])
def api_login_verify():
    """提交短信验证码。"""
    payload = request.get_json(silent=True) or {}
    session_id = payload.get("session_id", "").strip()
    sms_code = payload.get("sms_code", "").strip()

    if not session_id or not sms_code:
        return jsonify({
            "success": False,
            "message": "请输入 session_id 和短信验证码"
        }), 400

    result = login_module.submit_sms_code(session_id, sms_code)
    return _build_login_response(result)


@app.route("/api/login/status", methods=["GET"])
def api_login_status():
    """检查登录状态"""
    phone = request.args.get("phone", "").strip()
    if not phone:
        return jsonify({"logged_in": False})

    state_file = login_module.DATA_DIR / f"ctyun_state_{phone}.json"
    logged_in = state_file.exists()

    return jsonify({
        "logged_in": logged_in,
        "phone": phone
    })


def _build_login_response(result: login_module.LoginResult):
    if result.success and result.raw_json:
        return Response(result.raw_json, status=200, mimetype="application/json")

    status_code = 202 if result.data.get("require_sms_code") else 400
    response = {
        "success": result.success,
        "message": result.message,
    }
    if result.data:
        response["data"] = result.data
    return jsonify(response), status_code


if __name__ == "__main__":
    print("=" * 50)
    print("  天翼云登录服务")
    print("  访问地址: http://localhost:5000")
    print("=" * 50)
    app.run(host="0.0.0.0", port=5000, debug=True, threaded=True)
