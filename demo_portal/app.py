"""Realistic 2FA Authentication Portal for Co-Browsing Demo.

Simulates a high-security cloud dashboard requiring a 2-factor authentication
challenge. Used to demonstrate Hermes CoBrowse dual-control handoff end-to-end.
"""

from urllib.parse import parse_qs
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, RedirectResponse

portal_app = FastAPI(title="Apex Cloud Secure Portal")

HTML_LOGIN = """
<!DOCTYPE html>
<html>
<head>
  <title>Apex Cloud — Authenticate</title>
  <style>
    body { font-family: -apple-system, BlinkMacSystemFont, sans-serif; background: #0f172a; color: #f8fafc; display: flex; align-items: center; justify-content: center; height: 100vh; margin: 0; }
    .card { background: #1e293b; border: 1px solid #334155; border-radius: 12px; padding: 32px; width: 360px; box-shadow: 0 10px 30px rgba(0,0,0,0.5); }
    h2 { margin-top: 0; color: #38bdf8; font-size: 20px; }
    p { font-size: 13px; color: #94a3b8; margin-bottom: 20px; }
    label { display: block; font-size: 12px; font-weight: 600; margin-bottom: 6px; color: #cbd5e1; }
    input { width: 100%; box-sizing: border-box; padding: 10px 12px; background: #0f172a; border: 1px solid #475569; border-radius: 6px; color: #f8fafc; font-size: 14px; margin-bottom: 16px; }
    input:focus { border-color: #38bdf8; outline: none; }
    button { width: 100%; padding: 10px; background: #0284c7; color: white; border: none; border-radius: 6px; font-weight: 600; cursor: pointer; font-size: 14px; transition: background 0.2s; }
    button:hover { background: #0369a1; }
    .badge { display: inline-block; background: #0369a1; color: #e0f2fe; font-size: 11px; padding: 2px 8px; border-radius: 4px; margin-bottom: 12px; }
  </style>
</head>
<body>
  <div class="card">
    <div class="badge">SECURE ENCLAVE</div>
    <h2>Apex Cloud Console</h2>
    <p>Sign in to orchestrate production clusters</p>
    <form action="/login" method="post">
      <label>Email Address</label>
      <input type="email" id="email" name="email" value="admin@nousresearch.com" required />
      <label>Password</label>
      <input type="password" id="password" name="password" value="••••••••••••" required />
      <button type="submit" id="btnLogin">Sign In with Credentials</button>
    </form>
  </div>
</body>
</html>
"""

HTML_2FA = """
<!DOCTYPE html>
<html>
<head>
  <title>Apex Cloud — Two-Factor Verification</title>
  <style>
    body { font-family: -apple-system, BlinkMacSystemFont, sans-serif; background: #0f172a; color: #f8fafc; display: flex; align-items: center; justify-content: center; height: 100vh; margin: 0; }
    .card { background: #1e293b; border: 1px solid #f59e0b; border-radius: 12px; padding: 32px; width: 380px; box-shadow: 0 10px 30px rgba(245,158,11,0.2); }
    h2 { margin-top: 0; color: #fbbf24; font-size: 20px; display: flex; align-items: center; gap: 8px; }
    p { font-size: 13px; color: #94a3b8; line-height: 1.5; margin-bottom: 20px; }
    .alert-box { background: rgba(245,158,11,0.1); border: 1px solid rgba(245,158,11,0.3); padding: 12px; border-radius: 6px; font-size: 12px; color: #fde68a; margin-bottom: 20px; }
    label { display: block; font-size: 12px; font-weight: 600; margin-bottom: 6px; color: #cbd5e1; }
    input { width: 100%; box-sizing: border-box; padding: 12px; background: #0f172a; border: 2px solid #f59e0b; border-radius: 6px; color: #f8fafc; font-size: 20px; letter-spacing: 6px; text-align: center; font-family: monospace; font-weight: 700; margin-bottom: 16px; }
    input:focus { outline: none; box-shadow: 0 0 12px rgba(245,158,11,0.4); }
    button { width: 100%; padding: 12px; background: #f59e0b; color: #000; border: none; border-radius: 6px; font-weight: 700; cursor: pointer; font-size: 14px; transition: background 0.2s; }
    button:hover { background: #d97706; }
    .otp-hint { font-size: 11px; color: #64748b; text-align: center; margin-top: 10px; }
  </style>
</head>
<body>
  <div class="card">
    <h2><span>🛡️</span> Security Verification</h2>
    <p>An authentication challenge was triggered for your device.</p>
    <div class="alert-box">
      <strong>Authenticator Prompt:</strong> Enter the verification code <code>849201</code> sent to your mobile device.
    </div>
    <form action="/verify-2fa" method="post">
      <label>6-Digit Verification Code</label>
      <input type="text" id="otpCode" name="code" maxlength="6" placeholder="000000" autofocus required />
      <button type="submit" id="btnVerify">Verify & Confirm Identity</button>
    </form>
    <div class="otp-hint">Protected by Apex Hardware Token Security</div>
  </div>
</body>
</html>
"""

HTML_DASHBOARD = """
<!DOCTYPE html>
<html>
<head>
  <title>Apex Cloud — Production Console</title>
  <style>
    body { font-family: -apple-system, BlinkMacSystemFont, sans-serif; background: #0b0f19; color: #f8fafc; margin: 0; padding: 32px; }
    .header { display: flex; justify-content: space-between; align-items: center; border-bottom: 1px solid #1e293b; padding-bottom: 16px; margin-bottom: 32px; }
    .title { font-size: 22px; font-weight: 700; color: #38bdf8; }
    .user { font-size: 13px; color: #10b981; background: rgba(16,185,129,0.1); border: 1px solid rgba(16,185,129,0.3); padding: 4px 10px; border-radius: 20px; }
    .grid { display: grid; grid-template-columns: repeat(3, 1fr); gap: 20px; margin-bottom: 32px; }
    .card { background: #131d31; border: 1px solid #1e293b; border-radius: 8px; padding: 20px; }
    .card-title { font-size: 12px; color: #94a3b8; font-weight: 600; margin-bottom: 8px; text-transform: uppercase; }
    .card-metric { font-size: 26px; font-weight: 700; color: #f8fafc; }
    .deploy-section { background: #131d31; border: 1px solid #334155; border-radius: 8px; padding: 24px; }
    .btn-deploy { background: linear-gradient(135deg, #10b981, #059669); color: white; border: none; padding: 12px 24px; border-radius: 6px; font-weight: 700; font-size: 14px; cursor: pointer; box-shadow: 0 0 15px rgba(16,185,129,0.3); }
    .btn-deploy:hover { background: #059669; }
    .status-deployed { display: none; margin-top: 16px; padding: 12px; background: rgba(16,185,129,0.1); border: 1px solid #10b981; border-radius: 6px; color: #34d399; font-weight: 600; }
  </style>
</head>
<body>
  <div class="header">
    <div class="title">⚡ Apex Cloud Infrastructure</div>
    <div class="user">● Authenticated as: admin@nousresearch.com</div>
  </div>

  <div class="grid">
    <div class="card">
      <div class="card-title">Cluster Nodes</div>
      <div class="card-metric">64 Nodes</div>
    </div>
    <div class="card">
      <div class="card-title">Compute Latency</div>
      <div class="card-metric">1.2 ms</div>
    </div>
    <div class="card">
      <div class="card-title">Security Status</div>
      <div class="card-metric" style="color: #10b981;">2FA Verified</div>
    </div>
  </div>

  <div class="deploy-section">
    <h3>Production Workload Deployment</h3>
    <p style="color: #94a3b8; font-size: 13px; margin-bottom: 20px;">Ready to trigger autonomous rolling deployment of Hermes 3 cluster nodes.</p>
    <button class="btn-deploy" id="btnDeploy" onclick="document.getElementById('deployStatus').style.display='block'; document.getElementById('btnDeploy').innerText='Deployment Active';">
      🚀 Deploy Hermes Cluster
    </button>
    <div class="status-deployed" id="deployStatus">
      ✔ SUCCESS: 64 Hermes agent cluster nodes provisioned and healthy!
    </div>
  </div>
</body>
</html>
"""


@portal_app.get("/", response_class=HTMLResponse)
def get_home():
    return HTML_LOGIN


@portal_app.get("/login", response_class=HTMLResponse)
def get_login():
    return HTML_LOGIN


@portal_app.post("/login")
async def post_login(request: Request):
    body = (await request.body()).decode("utf-8", errors="ignore")
    form = parse_qs(body)
    email = form.get("email", [""])[0].strip()
    password = form.get("password", [""])[0]
    if not email or not password:
        return HTMLResponse("<h3>Email and password required. <a href='/login'>Try again</a></h3>", status_code=400)
    return RedirectResponse(url="/2fa", status_code=303)


@portal_app.get("/2fa", response_class=HTMLResponse)
def get_2fa():
    return HTML_2FA


@portal_app.post("/verify-2fa")
async def post_2fa(request: Request):
    body = (await request.body()).decode("utf-8", errors="ignore")
    form = parse_qs(body)
    code = form.get("code", [""])[0].strip()
    if code == "849201":
        return RedirectResponse(url="/dashboard", status_code=303)
    return HTMLResponse(
        "<h3>Invalid 2FA code (expected 849201). <a href='/2fa'>Try again</a></h3>",
        status_code=400,
    )


@portal_app.get("/dashboard", response_class=HTMLResponse)
def get_dashboard():
    return HTML_DASHBOARD
