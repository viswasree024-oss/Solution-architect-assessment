"""Mint a test JWT:  python make_token.py admin   |   python make_token.py viewer"""
import sys, time
import jwt
from gateway import JWT_SECRET
role = sys.argv[1] if len(sys.argv) > 1 else "viewer"
print(jwt.encode({"sub": "demo-user", "role": role, "exp": int(time.time()) + 3600}, JWT_SECRET, algorithm="HS256"))
