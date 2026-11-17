from . import emit_patch, implement, prefetch, prepare, repair, reproduce, review, validate

# Execution order is fixed by deterministic code (stage 0: straight through).
PIPELINE = [
    ("prepare", prepare.run),
    ("prefetch", prefetch.run),
    ("reproduce", reproduce.run),
    ("implement", implement.run),
    ("validate", validate.run),
    ("repair", repair.run),
    ("review", review.run),
    ("emit_patch", emit_patch.run),
]
