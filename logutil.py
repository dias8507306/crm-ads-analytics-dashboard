"""Печать в журнал, которая не падает на консоли Windows (cp1251 не знает «→» и казахских букв)."""
import sys


def log(message):
    try:
        print(message, flush=True)
    except UnicodeEncodeError:
        enc = sys.stdout.encoding or "ascii"
        print(message.encode(enc, "replace").decode(enc), flush=True)
