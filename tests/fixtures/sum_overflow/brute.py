import sys


def main() -> None:
    data = sys.stdin.read().split()
    n = int(data[0])
    values = [int(v) for v in data[1 : 1 + n]]
    print(sum(values))


main()
