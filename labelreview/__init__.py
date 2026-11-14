"""Local-first prepackaged-food label review workbench.

The package is intentionally flat: modules are grouped by role in their names
(`audit`, `evidence_checklist`, `report_plan`, ...) rather than nested packages,
because the judgement pipeline reads top-to-bottom in one process and every
module is a plain function library with no framework behind it.
"""

__version__ = "1.4.2"
