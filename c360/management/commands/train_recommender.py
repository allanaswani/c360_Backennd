"""Train the next-best-product propensity models from the live warehouse.

    python manage.py train_recommender --sample 80000

Requires live mode (a reachable Trino warehouse) — the model learns from the real
whole-book ownership pattern. Writes boosters + manifest to c360/ml/models/ and prints
the per-product AUC / precision@10%. Re-run nightly (or after a data refresh); the API
picks up new models on the next request (cache is reset here).
"""
from __future__ import annotations

import time

from django.core.management.base import BaseCommand, CommandError

from c360.ml import features as F
from c360.ml.model import reset_model_cache
from c360.ml.train import train_all
from c360.warehouse.factory import data_mode, get_gateway


class Command(BaseCommand):
    help = 'Train the LightGBM next-best-product models from the live warehouse.'

    def add_arguments(self, parser):
        parser.add_argument('--sample', type=int, default=80_000,
                            help='Number of depositing customers to sample for training.')
        parser.add_argument('--use-feedback', action='store_true',
                            help='Blend recorded recommendation outcomes (the feedback loop) '
                                 'into training as real conversion labels.')

    def handle(self, *args, **opts):
        if data_mode() != 'live':
            raise CommandError('train_recommender needs live mode (set C360_DATA_MODE=live).')
        gateway = get_gateway()

        self.stdout.write(f'Extracting up to {opts["sample"]:,} training rows from the warehouse…')
        t0 = time.time()
        rows = F.extract_training_rows(gateway, sample=opts['sample'])
        self.stdout.write(f'  got {len(rows):,} rows in {time.time() - t0:.1f}s')
        if not rows:
            raise CommandError('No rows extracted — is the warehouse reachable?')

        feedback = None
        if opts['use_feedback']:
            from c360.ml.feedback_labels import labelled_rows
            feedback = labelled_rows(gateway)
            total = sum(len(v) for v in feedback.values())
            self.stdout.write(f'  blending {total} recorded outcome label(s) across {len(feedback)} product(s)')

        self.stdout.write('Training per-product models…')
        t1 = time.time()
        manifest = train_all(rows, feedback_labels=feedback)
        reset_model_cache()

        self.stdout.write(self.style.SUCCESS(
            f'\nTrained {manifest["n_products_trained"]} products in {time.time() - t1:.1f}s '
            f'(mean AUC {manifest["mean_auc"]}):'))
        for prod, meta in manifest['products'].items():
            if meta.get('trained'):
                self.stdout.write(
                    f'  {prod:<14} AUC={meta["auc"]:.3f}  P@10%={meta["precision_at_10pct"]:.3f}  '
                    f'(held by {meta["rate"]*100:.1f}% of sample)')
            else:
                self.stdout.write(f'  {prod:<14} skipped (positives={meta.get("positives")})')
        self.stdout.write(self.style.SUCCESS('\nModels saved to c360/ml/models/. The API will use them on the next request.'))
