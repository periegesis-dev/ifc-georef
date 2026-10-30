import os

bind = f"{os.getenv('HOST', '0.0.0.0')}:{os.getenv('PORT', '5002')}"

# One worker process: it owns the background job pool. Requests are served by
# threads; heavy IFC work runs in separate worker processes (see ifcgeoref/jobs.py).
workers = 1
worker_class = "gthread"
threads = int(os.getenv("WEB_THREADS", "8"))
timeout = int(os.getenv("WEB_TIMEOUT", "600"))
graceful_timeout = 30

accesslog = "-"
errorlog = "-"
loglevel = os.getenv("LOG_LEVEL", "info")
# %(U)s is the path without the query string, so tokens passed as URL
# parameters never end up in the logs.
access_log_format = '%(h)s "%(m)s %(U)s %(H)s" %(s)s %(B)s %(M)sms'
