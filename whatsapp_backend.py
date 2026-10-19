
import hashlib
import hmac
import os

from flask import Flask, request, jsonify

app = Flask(__name__)

VERIFY_TOKEN = os.environ.get("WHATSAPP_VERIFY_TOKEN", "")
APP_SECRET = os.environ.get("META_APP_SECRET", "")


@app.get("/")
def health():
    return jsonify({
        "service": "Malay Tyres WhatsApp backend",
        "status": "running"
    }), 200


@app.get("/oauth/callback")
def oauth_callback():
    # Meta may redirect here with an authorization code.
    # Do not expose or log the code or any access token.
    error = request.args.get("error")
    if error:
        return jsonify({
            "status": "authorization_not_completed",
            "message": "Return to the Malay Tyres agent and try again."
        }), 400

    code = request.args.get("code")
    if not code:
        return jsonify({
            "status": "callback_ready",
            "message": "OAuth callback endpoint is reachable."
        }), 200

    # Secure server-side code exchange must be implemented
    # before this callback can complete Embedded Signup.
    return jsonify({
        "status": "callback_received",
        "message": "Authorization callback received; code exchange is not configured yet."
    }), 200


@app.get("/webhook")
def verify_webhook():
    mode = request.args.get("hub.mode", "")
    token = request.args.get("hub.verify_token", "")
    challenge = request.args.get("hub.challenge", "")

    if (
        mode == "subscribe"
        and VERIFY_TOKEN
        and hmac.compare_digest(token, VERIFY_TOKEN)
    ):
        return challenge, 200, {"Content-Type": "text/plain"}

    return "Webhook verification failed", 403


@app.post("/webhook")
def receive_webhook():
    raw_body = request.get_data()
    signature = request.headers.get("X-Hub-Signature-256", "")

    if not APP_SECRET:
        return jsonify({"error": "Webhook not configured"}), 503

    expected = "sha256=" + hmac.new(
        APP_SECRET.encode("utf-8"),
        raw_body,
        hashlib.sha256
    ).hexdigest()

    if not hmac.compare_digest(signature, expected):
        return jsonify({"error": "Invalid signature"}), 401

    try:
        payload = request.get_json(force=True)
    except Exception:
        return jsonify({"error": "Invalid JSON"}), 400

    if payload.get("object") == "whatsapp_business_account":
        for entry in payload.get("entry", []):
            for change in entry.get("changes", []):
                value = change.get("value", {})
                message_count = len(value.get("messages", []))
                status_count = len(value.get("statuses", []))

                if message_count or status_count:
                    app.logger.info(
                        "WhatsApp event received: messages=%s statuses=%s",
                        message_count,
                        status_count,
                    )

    return jsonify({"received": True}), 200
