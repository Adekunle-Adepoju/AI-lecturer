from django.core.management.base import BaseCommand
from battle.generation import run_battle_question_generation


class Command(BaseCommand):
    help = (
        "Generate battle-mode MCQ questions from non-empty SlideTopicChunks "
        "for 300L/400L courses. Resumable — already-COMPLETED chunks are "
        "skipped by default. All generated questions land as pending_review "
        "and require staff approval before entering the servable pool."
    )

    def add_arguments(self, parser):
        parser.add_argument("--per-chunk", type=int, default=8)
        parser.add_argument("--sleep", type=float, default=3.5)
        parser.add_argument("--course", type=str, default=None)
        parser.add_argument("--retry-failed", action="store_true")
        parser.add_argument("--limit", type=int, default=None)

    def handle(self, *args, **options):
        result = run_battle_question_generation(
            per_chunk=options["per_chunk"],
            sleep_seconds=options["sleep"],
            course_filter=options["course"],
            retry_failed=options["retry_failed"],
            limit=options["limit"],
            log=lambda msg: self.stdout.write(msg),
        )
        self.stdout.write(self.style.SUCCESS(
            f"\nDone. {result['questions_generated']} question(s) generated across "
            f"{result['chunks_processed'] - result['chunks_failed']} chunk(s), "
            f"{result['chunks_failed']} chunk(s) failed."
        ))