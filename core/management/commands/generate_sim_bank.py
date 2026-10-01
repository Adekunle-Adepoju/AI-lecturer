from django.core.management.base import BaseCommand
from core.simulator_bank import run_bank_generation


class Command(BaseCommand):
    help = "Generate and verify the simulator question bank for one course."

    def add_arguments(self, parser):
        parser.add_argument("course_code")
        parser.add_argument("--level", required=True, help="e.g. 400")
        parser.add_argument("--topic", action="append", dest="topics",
                            help="Limit to this topic (repeatable)")
        parser.add_argument("--force", action="store_true",
                            help="Regenerate even if the source is unchanged")
        parser.add_argument("--auto-approve", action="store_true",
                            help="Save verified questions as approved instead of pending_review")
        parser.add_argument("--limit", type=int, help="Max topics to process this run")

    def handle(self, *args, **opts):
        summary = run_bank_generation(
            opts["course_code"], opts["level"],
            topics=opts["topics"], force=opts["force"],
            auto_approve=opts["auto_approve"], limit=opts["limit"],
            log=self.stdout.write,
        )
        self.stdout.write(self.style.SUCCESS(f"Done: {summary}"))