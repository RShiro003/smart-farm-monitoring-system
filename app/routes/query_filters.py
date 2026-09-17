"""Request filter parsing shared by the event table and its CSV export."""


def event_filters(args):
    filters = {
        field: (args.get(field) or "").strip()
        for field in ("status", "metric", "date", "time_from", "time_to",
                      "date_from", "date_to")
    }
    for field in ("status", "metric"):
        filters[field] = filters[field] or None
    return filters
