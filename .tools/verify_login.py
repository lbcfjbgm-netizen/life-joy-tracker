import re
import sys

import requests

BASE = "http://127.0.0.1:8001"
s = requests.Session()
r = s.get(BASE + "/accounts/login/", timeout=30)
print("login page:", r.status_code)
m = re.search(r'name="csrfmiddlewaretoken" value="([^"]+)"', r.text)
if not m:
    print("NO CSRF TOKEN")
    sys.exit(1)
token = m.group(1)
r2 = s.post(
    BASE + "/accounts/login/",
    data={
        "csrfmiddlewaretoken": token,
        "login": "admin",
        "password": "admin123456",
    },
    headers={"Referer": BASE + "/accounts/login/"},
    timeout=30,
    allow_redirects=False,
)
print("login POST:", r2.status_code, "->", r2.headers.get("Location"))
r3 = s.get(BASE + "/", timeout=30, allow_redirects=False)
print("home after login:", r3.status_code, "->", r3.headers.get("Location"))
if r3.status_code == 200:
    print("logged-in markers:", "logout" in r3.text.lower(), "admin" in r3.text.lower())
