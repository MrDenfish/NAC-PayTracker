"""FastAPI + HTMX + Jinja2 web UI for the NAC pay tracker.

Desktop-first per spec §13. The seven screens (Dashboard, Calendar, Day
detail, Pay breakdown, Compare to pay stub, Discrepancies, Settings)
ship across multiple milestones. This module is the Dashboard milestone.

Run locally with:

    uvicorn nac_pay.app.main:app --reload
"""
