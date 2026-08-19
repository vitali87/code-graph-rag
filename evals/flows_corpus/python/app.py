import os


def leak():
    secret = os.getenv("TOKEN")
    print(secret)


def safe():
    fixed = "constant"
    return fixed
