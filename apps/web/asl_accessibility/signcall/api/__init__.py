"""
The HTTP seam. The frontend (built separately, not in this repo) POSTs its
user-input JSON here and polls for the result.

Deliberately thin: validation is workflow/user_input.py's job, the run is
workflow/appointment.py's job, and this package only turns an HTTP request
into a call to those. Nothing about the workflow knows it's being driven over
HTTP.
"""
