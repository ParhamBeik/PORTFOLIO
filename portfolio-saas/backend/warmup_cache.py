import os
import django
import time

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
django.setup()

from portfolio.services.returns import daily_returns_matrix
from portfolio.services.optimization import _efficient_frontier

print("Warming up daily returns matrix cache...")
start = time.time()
df, excluded = daily_returns_matrix(history_days=365)
print(f"Daily returns matrix cache warmed up in {time.time() - start:.2f}s. Shape: {df.shape}")

print("Warming up efficient frontier cache...")
start = time.time()
res = _efficient_frontier(n_points=30)
print(f"Efficient frontier cache warmed up in {time.time() - start:.2f}s. Points: {len(res.get('frontier', []))}")
