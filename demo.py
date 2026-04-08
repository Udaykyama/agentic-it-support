import requests
import time

TICKETS = [
    {"title": "VPN not connecting", "description": "I can't connect to the company VPN. It times out every time.", "submitter": "alice@company.com"},
    {"title": "VPN timeout issue", "description": "VPN keeps disconnecting after 2 minutes. Happens on wifi and ethernet.", "submitter": "bob@company.com"},
    {"title": "Cannot connect to VPN", "description": "Getting error code 800 when trying to connect to VPN from home.", "submitter": "carol@company.com"},
    {"title": "VPN drops connection", "description": "Every time I connect to VPN it drops after a few minutes.", "submitter": "dan@company.com"},
    {"title": "Password reset needed", "description": "My password expired and I am locked out of my account.", "submitter": "dave@company.com"},
    {"title": "Can't log in, password expired", "description": "System says my password expired 3 days ago. Need reset.", "submitter": "eve@company.com"},
    {"title": "Account locked out", "description": "Too many failed login attempts, account is locked.", "submitter": "frank@company.com"},
    {"title": "Forgot password", "description": "I forgot my password and need a reset link sent to my email.", "submitter": "grace@company.com"},
    {"title": "Laptop won't turn on", "description": "Pressed power button, nothing happens. Was working yesterday.", "submitter": "henry@company.com"},
    {"title": "Blue screen on startup", "description": "Computer crashes with blue screen every time I try to boot.", "submitter": "iris@company.com"},
    {"title": "No internet connection", "description": "Ethernet connected but no internet access. Other devices work fine.", "submitter": "jack@company.com"},
    {"title": "Slow internet in conference room", "description": "WiFi is very slow in room 3B, can't run video calls.", "submitter": "karen@company.com"},
    {"title": "Can't access shared drive", "description": "Getting permission denied when trying to open the shared network drive.", "submitter": "leo@company.com"},
    {"title": "Software not installing", "description": "Tried to install the new analytics tool but getting an error halfway through.", "submitter": "maya@company.com"},
    {"title": "Outlook keeps crashing", "description": "Outlook crashes every time I try to open an attachment.", "submitter": "nick@company.com"},
]

BASE_URL = "http://localhost:5000"

print("=" * 50)
print("SENDING TICKETS TO SYSTEM")
print("=" * 50)

for t in TICKETS:
    r = requests.post(f"{BASE_URL}/ticket", json=t)
    result = r.json()
    action = result.get("action", "unknown").upper()
    print(f"[{action}] {t['title']}")
    time.sleep(1)

print("\n" + "=" * 50)
print("GENERATING RUNBOOKS FROM PATTERNS")
print("=" * 50)

r = requests.get(f"{BASE_URL}/generate-runbooks")
data = r.json()
for rb in data.get("generated", []):
    print(f"Generated: {rb}")

print("\n" + "=" * 50)
print("RUNBOOKS IN KNOWLEDGE BASE")
print("=" * 50)

r = requests.get(f"{BASE_URL}/runbooks")
data = r.json()
for rb in data.get("runbooks", []):
    print(f"  - {rb}")

print("\nDone! Check data/runbooks/ folder for generated markdown files.")