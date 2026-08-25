import sys
from app import error_rate

expected = float(open('expected_rate.txt').read().strip())
got = error_rate('events.log')
print(f'error_rate -> {got:.6f}, expected {expected:.6f}')
assert abs(got - expected) < 0.0005, f'FAIL: {got} != {expected}'
print('PASS')
