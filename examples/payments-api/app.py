def error_rate(path):
    """Fraction of log entries that are ERROR level."""
    total = 0
    errs = 0
    with open(path, encoding='utf-8') as fh:
        for line in fh:
            total += 1
            if ' ERROR ' in line:
                errs += 1
    return errs / total
