import os

if "PROM_DSN" not in os.environ:
    os.environ["PROM_DSN"] = "sqlite://test.db"
