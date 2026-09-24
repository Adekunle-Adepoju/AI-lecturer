import csv, sys
from django.core.management.base import BaseCommand
from engagement import reports


class Command(BaseCommand):
    help = "Engagement metrics: DAU/WAU/MAU, per-module read time, TTS."

    def add_arguments(self, parser):
        parser.add_argument("--days", type=int, default=30)
        parser.add_argument("--csv", action="store_true", help="Per-module table as CSV on stdout")

    def handle(self, *args, **o):
        days = o["days"]
        if o["csv"]:
            rows = reports.module_report(days)
            if rows:
                w = csv.DictWriter(sys.stdout, fieldnames=rows[0].keys())
                w.writeheader(); w.writerows(rows)
            return
        self.stdout.write(f"Headline: {reports.headline()}")
        self.stdout.write(f"TTS ({days}d): {reports.tts_summary(days)}")
        self.stdout.write("Daily active users:")
        for r in reports.dau_series(days):
            self.stdout.write(f"  {r['date']}  {r['users']}")
        self.stdout.write("Modules (avg minutes per reader):")
        for r in reports.module_report(days):
            self.stdout.write(f"  {r['course_code']} wk{r['week_number']} {r['topic_name'][:40]:40} "
                              f"readers={r['readers']} read={r['avg_read_min']}m listen={r['avg_listen_min']}m")