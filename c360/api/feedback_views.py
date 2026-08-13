"""Recommendation outcome-logging — the feedback loop.

RMs mark what happened to a recommendation (pitched / accepted / declined / not
relevant). Those outcomes are the real conversion labels the model needs; until they
accumulate the model runs on ownership look-alikes. This module records outcomes,
lists them for a customer (so the panel can show what's already marked), and reports
the aggregate stats — including acceptance-by-score-decile, which validates whether
higher model propensity actually converts better.
"""
from __future__ import annotations

from django.db.models import Avg, Count, Q
from rest_framework import serializers, status
from rest_framework.permissions import IsAuthenticated
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.views import APIView

from ..models import RecommendationFeedback


class FeedbackSerializer(serializers.ModelSerializer):
    recorded_by_name = serializers.SerializerMethodField()

    class Meta:
        model = RecommendationFeedback
        fields = ('id', 'cust_id', 'product', 'product_name', 'domain', 'score',
                  'rule_id', 'engine_version', 'outcome', 'note',
                  'recorded_by_name', 'created_at', 'updated_at')
        read_only_fields = ('id', 'recorded_by_name', 'created_at', 'updated_at')

    def get_recorded_by_name(self, obj):
        u = obj.recorded_by
        return (u.get_full_name() or u.username) if u else None


class FeedbackView(APIView):
    """GET ?cust_id=… → outcomes already recorded for that customer (panel state).
    POST → record/update an outcome (one per customer+product+RM, upserted)."""

    permission_classes = [IsAuthenticated]

    def get(self, request: Request):
        qs = RecommendationFeedback.objects.all()
        cust_id = request.query_params.get('cust_id')
        if cust_id:
            qs = qs.filter(cust_id=cust_id)
        # An RM sees their own marks for the panel; management sees all.
        mine = request.query_params.get('mine')
        if mine in ('1', 'true'):
            qs = qs.filter(recorded_by=request.user)
        qs = qs.select_related('recorded_by').order_by('-updated_at')[:200]
        return Response({'results': FeedbackSerializer(qs, many=True).data})

    def post(self, request: Request):
        data = request.data
        cust_id = str(data.get('cust_id', '')).strip()
        product = str(data.get('product', '')).strip()
        outcome = str(data.get('outcome', '')).strip()
        valid = {c for c, _ in RecommendationFeedback.OUTCOME_CHOICES}
        if not cust_id or not product or outcome not in valid:
            return Response({'error': {'status': 400, 'detail': 'cust_id, product and a valid outcome are required.'}},
                            status=status.HTTP_400_BAD_REQUEST)

        # Upsert on (customer, product, this RM): re-marking updates in place.
        score = data.get('score')
        obj, _created = RecommendationFeedback.objects.update_or_create(
            cust_id=cust_id, product=product, recorded_by=request.user,
            defaults={
                'product_name': str(data.get('product_name', ''))[:120],
                'domain': str(data.get('domain', 'HFCB'))[:32],
                'score': float(score) if score not in (None, '') else None,
                'rule_id': str(data.get('rule_id', ''))[:32],
                'engine_version': str(data.get('engine_version', ''))[:32],
                'outcome': outcome,
                'note': str(data.get('note', '')),
            })
        code = status.HTTP_201_CREATED if _created else status.HTTP_200_OK
        return Response(FeedbackSerializer(obj).data, status=code)


class FeedbackStatsView(APIView):
    """Aggregate outcome stats — the model-validation surface.

    ``acceptance_by_decile`` groups labelled (accepted/declined) recommendations by the
    model's score band and reports the acceptance rate in each: if the model is any
    good, acceptance rises with the band. That's the honest, outcome-based check that
    replaces a fabricated accuracy number."""

    permission_classes = [IsAuthenticated]

    def get(self, request: Request):
        qs = RecommendationFeedback.objects.all()
        by_outcome = {row['outcome']: row['n'] for row in qs.values('outcome').annotate(n=Count('id'))}
        total = sum(by_outcome.values())
        labelled = qs.filter(outcome__in=list(RecommendationFeedback.POSITIVE_OUTCOMES
                                              | RecommendationFeedback.NEGATIVE_OUTCOMES))
        n_labelled = labelled.count()
        n_accepted = labelled.filter(outcome__in=list(RecommendationFeedback.POSITIVE_OUTCOMES)).count()
        acceptance_rate = round(n_accepted / n_labelled, 4) if n_labelled else None

        # Acceptance rate by model-score band (only rows that carry a score).
        bands = [(0.0, 0.2), (0.2, 0.4), (0.4, 0.6), (0.6, 0.8), (0.8, 1.01)]
        by_band = []
        scored = labelled.filter(score__isnull=False)
        for lo, hi in bands:
            band = scored.filter(score__gte=lo, score__lt=hi)
            n = band.count()
            acc = band.filter(outcome__in=list(RecommendationFeedback.POSITIVE_OUTCOMES)).count()
            by_band.append({
                'band': f'{int(lo*100)}-{int(min(hi,1.0)*100)}%',
                'n': n,
                'acceptance_rate': round(acc / n, 4) if n else None,
            })

        return Response({
            'total': total,
            'by_outcome': by_outcome,
            'labelled': n_labelled,
            'accepted': n_accepted,
            'acceptance_rate': acceptance_rate,
            'acceptance_by_score_band': by_band,
            'ready_to_retrain': n_labelled >= 500,   # rough floor for a first real retrain
        })
