"""TaskPanel integration (public_html/taskPanel). TaskPanel is the authority for staff tasks.

Commands:  phone -> Django -> TaskPanel  (signed, idempotent, committed receipt = success)
Events:    TaskPanel -> Django           (pull change feed; updates the projection, never re-executes commands)
"""
