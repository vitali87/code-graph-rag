import os


def leak():
    value = os.getenv("TOKEN")
    print(value)


def safe():
    fixed = "constant"
    print(fixed)
