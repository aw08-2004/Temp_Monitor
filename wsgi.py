from app import application  # noqa: F401  (waitress serves `wsgi:application`)

# The hub deliberately does NOT self-report its own host temperature anymore.
# The companion agent (companion.py) runs on the hub machine too and reports it
# with full sensor data, so also starting the built-in local_logger here would
# double-report the hub's hostname -- one stream with sensors, one without --
# which made the dashboard's CPU/GPU Load & Clock flicker. If you ever run the
# hub on a box that has no companion, re-enable it with:
#     from app import start_local_logger
#     start_local_logger()
