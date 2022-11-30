
import time
from typing import Iterable
from contextlib import contextmanager
import logging as log


@contextmanager
def timeit_context(name):
    start_time = time.time()
    yield
    elapsed_time = time.time() - start_time
    log.info('[{}] finished in {} ms'.format(name, int(elapsed_time * 1_000)))


def clamp(n, _min, _max):
    return max(_min, min(_max, n))


def chunk(iter: Iterable, n: int):
    for i in range(0, len(iter), n):
        yield iter[i:(i+n)]
