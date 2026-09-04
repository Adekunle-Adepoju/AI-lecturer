from django import template
register = template.Library()

@register.filter
def enumerate_zip(questions):
    return enumerate(questions)

@register.filter
def pluralize_marks(value):
    return "s" if value != 1 else ""