import sys

data = sys.stdin.read().split()
if not data:
    print("empty input", file=sys.stderr)
    sys.exit(1)

n = int(data[0])
if not 1 <= n <= 100000:
    print(f"n out of range: {n}", file=sys.stderr)
    sys.exit(1)

values = data[1:]
if len(values) != n:
    print(f"declared n={n} but got {len(values)} values", file=sys.stderr)
    sys.exit(1)

for v in values:
    x = int(v)
    if not 1 <= x <= 10**9:
        print(f"value out of range: {x}", file=sys.stderr)
        sys.exit(1)

sys.exit(0)
