#!/usr/bin/env python3
"""
DL Manager - Entry Point
"""
import uvicorn
from app.main import create_app

app = create_app()

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8899, log_level="info", timeout_keep_alive=65)
