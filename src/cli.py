"""
CLI entrypoint backward compatibility shim.
Forwards to src.presentation.cli.
"""
from src.presentation.cli import *
from src.presentation.cli import app

if __name__ == "__main__":
    app()
