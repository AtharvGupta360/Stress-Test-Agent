import random
import sys

seed = int(sys.argv[1])
size = int(sys.argv[2])
random.seed(seed)

n = size
MAX_V = 10**9

# Bias toward extremes about a third of the time: uniform random alone rarely
# stacks enough large values to overflow a 32-bit accumulator.
roll = random.random()
if roll < 0.2:
    values = [MAX_V] * n
elif roll < 0.35:
    values = [1] * n
else:
    values = [random.randint(1, MAX_V) for _ in range(n)]

print(n)
print(" ".join(str(v) for v in values))
