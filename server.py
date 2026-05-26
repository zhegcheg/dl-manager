#!/usr/bin/env python3
"""
DL Manager - Entry Point
"""
import uvicorn
from app.main import create_app

app = create_app()

if __name__ == "__main__":
    import os
    port = int(os.environ.get("PORT", 8899))
    uvicorn.run(app, host="0.0.0.0", port=port, log_level="info", timeout_keep_alive=65)
