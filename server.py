import os

from exact_mcp.app import create_server

if __name__ == "__main__":
    create_server().run(transport="http", host="0.0.0.0", port=int(os.environ.get("PORT", "8000")))
