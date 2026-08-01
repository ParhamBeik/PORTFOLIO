import sys

file_path = "backend/marketdata/models.py"
with open(file_path, "r") as f:
    content = f.read()

new_model = """

class SymbolIntegrity(models.Model):
    \"\"\"Integrity gate checks per symbol.\"\"\"
    symbol = models.CharField(max_length=64, db_index=True, unique=True)
    source = models.CharField(max_length=8, blank=True, default="")
    coverage_ratio = models.FloatField(default=0.0)
    max_gap_days = models.IntegerField(default=0)
    rejected_count = models.IntegerField(default=0)
    passes_gate = models.BooleanField(default=False, db_index=True)
    reason = models.CharField(max_length=255, blank=True, default="")
    computed_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["symbol"]
"""

if "class SymbolIntegrity" not in content:
    content += new_model

with open(file_path, "w") as f:
    f.write(content)

print("SymbolIntegrity added.")
