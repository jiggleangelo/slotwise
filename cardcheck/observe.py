import time


def observe(source, samples=60, sleep=time.sleep):
    if not 1 <= samples <= 3600:
        raise ValueError("Sample count is out of range.")
    result = []
    for index in range(samples):
        result.append(source.sample())
        if index + 1 < samples:
            sleep(1)
    return result
