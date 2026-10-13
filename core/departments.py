"""
core/departments.py

The one place that knows which departments Rovea teaches. Adding a department
means adding one entry here and running makemigrations (the field choices change).
No model imports, so models.py and forms.py can both import it safely.
"""

DEPARTMENTS = {
    "petroleum": {
        "label": "Petroleum & Gas Engineering",
        "faculty": "Engineering",
        "open": True,             # students can pick it at signup
        "siwes": ("400", "2"),    # (level, semester) of industrial training, or None
    },
    "cbg": {
        "label": "Cell Biology & Genetics",
        "faculty": "Science",
        "open": True,
        "siwes": None,            # not confirmed yet
    },
}

DEPARTMENT_CHOICES = [(k, d["label"]) for k, d in DEPARTMENTS.items()]
OPEN_DEPARTMENT_CHOICES = [(k, d["label"]) for k, d in DEPARTMENTS.items() if d["open"]]


def department_label(key):
    d = DEPARTMENTS.get(key)
    return d["label"] if d else key


def siwes_applies(department, level, semester):
    d = DEPARTMENTS.get(department)
    return bool(d and d["siwes"] and (level, semester) == d["siwes"])