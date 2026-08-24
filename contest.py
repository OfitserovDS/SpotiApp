
from collections import Counter

p = input()
v = input()

bulls = sum(s == g for s, g in zip(p, v))
p_count = Counter(p)
v_count = Counter(v)
summa = sum((p_count & v_count).values())

cows = summa - bulls

print(bulls)
print(cows)