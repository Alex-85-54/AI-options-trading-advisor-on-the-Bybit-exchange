"""
Dynamic threshold calculation and retrieval based on historical data.
"""
import logging
import math
from collections import Counter, defaultdict
from datetime import date, datetime, timedelta
from typing import Dict, Iterable, List, Optional, Tuple

from config import DYNAMIC_THRESHOLD_CONFIG, DTE_BINS, STRATEGY_CONFIG
from core.data.database import get_database

logger = logging.getLogger(__name__)


def _percentile(values: List[float], percentile: float) -> Optional[float]:
    """Compute percentile with linear interpolation."""
    if not values:
        return None
    if percentile <= 0:
        return min(values)
    if percentile >= 100:
        return max(values)
    values_sorted = sorted(values)
    k = (len(values_sorted) - 1) * (percentile / 100.0)
    f = math.floor(k)
    c = math.ceil(k)
    if f == c:
        return values_sorted[int(k)]
    return values_sorted[f] + (k - f) * (values_sorted[c] - values_sorted[f])


class DynamicThresholds:
    """Compute and retrieve dynamic strategy thresholds from historical data."""

    def __init__(self):
        self.db = get_database()
        self.enabled = DYNAMIC_THRESHOLD_CONFIG.get("enabled", True)
        self.lookback_days = DYNAMIC_THRESHOLD_CONFIG.get("lookback_days", 30)
        self.recalc_interval_hours = DYNAMIC_THRESHOLD_CONFIG.get("recalc_interval_hours", 24)
        self.min_sample_size = DYNAMIC_THRESHOLD_CONFIG.get("min_sample_size", 50)
        self.percentiles = DYNAMIC_THRESHOLD_CONFIG.get("percentiles", {})

    def _get_bucket_for_dte(self, days_to_expiration: int) -> Optional[str]:
        for bucket in DTE_BINS:
            min_dte = bucket["min"]
            max_dte = bucket["max"]
            if days_to_expiration < min_dte:
                continue
            if max_dte is None or days_to_expiration <= max_dte:
                return bucket["label"]
        return None

    def _get_bucket_range(self, dte_bucket: str) -> Optional[Tuple[int, Optional[int]]]:
        for bucket in DTE_BINS:
            if bucket["label"] == dte_bucket:
                return bucket["min"], bucket["max"]
        return None

    def _bucket_from_symbol(self, symbol: str) -> Optional[str]:
        parsed = self.db.parse_option_symbol(symbol)
        if not parsed or not parsed.get("expiration_date"):
            return None
        expiration_date = parsed["expiration_date"]
        days_to_expiration = (expiration_date - date.today()).days
        if days_to_expiration < 0:
            return None
        return self._get_bucket_for_dte(days_to_expiration)

    def _primary_bucket_from_options(self, options_data: Dict[str, Dict]) -> Optional[str]:
        buckets = []
        for symbol in options_data.keys():
            bucket = self._bucket_from_symbol(symbol)
            if bucket:
                buckets.append(bucket)
        if not buckets:
            return None
        return Counter(buckets).most_common(1)[0][0]

    def _parse_strike(self, symbol: str) -> Optional[float]:
        try:
            parts = symbol.split("-")
            if len(parts) >= 3:
                return float(parts[2])
        except (ValueError, IndexError):
            return None
        return None

    def _parse_option_type(self, symbol: str) -> Optional[str]:
        try:
            parts = symbol.split("-")
            if len(parts) >= 4:
                return parts[3].upper()
        except IndexError:
            return None
        return None

    def _compute_concentration(self, rows: List[Dict], greek_key: str) -> Optional[float]:
        greek_by_strike: Dict[float, float] = defaultdict(float)
        total_value = 0.0
        for row in rows:
            value = row.get(greek_key)
            if value is None or value <= 0:
                continue
            strike = self._parse_strike(row.get("symbol", ""))
            if strike is None:
                continue
            greek_by_strike[strike] += abs(value)
            total_value += abs(value)
        if total_value == 0:
            return None
        max_strike = max(greek_by_strike.items(), key=lambda x: x[1])[0] if greek_by_strike else None
        if max_strike is None:
            return None
        strikes = sorted(greek_by_strike.keys())
        try:
            max_idx = strikes.index(max_strike)
        except ValueError:
            return None
        start_idx = max(0, max_idx - 2)
        end_idx = min(len(strikes), max_idx + 3)
        concentrated_strikes = strikes[start_idx:end_idx]
        concentrated_value = sum(greek_by_strike[s] for s in concentrated_strikes)
        return concentrated_value / total_value if total_value > 0 else None

    def _compute_slice_metrics(self, rows: List[Dict]) -> Dict[str, Optional[float]]:
        call_deltas = []
        put_deltas = []
        call_sum = 0.0
        put_sum = 0.0
        for row in rows:
            delta = row.get("delta")
            if delta is None:
                continue
            option_type = self._parse_option_type(row.get("symbol", ""))
            if option_type == "C":
                call_deltas.append(abs(delta))
                call_sum += abs(delta)
            elif option_type == "P":
                put_deltas.append(abs(delta))
                put_sum += abs(delta)
        total_sum = call_sum + put_sum
        imbalance = None
        if total_sum > 0:
            imbalance = (call_sum - put_sum) / total_sum
        avg_call = sum(call_deltas) / len(call_deltas) if call_deltas else 0.0
        avg_put = sum(put_deltas) / len(put_deltas) if put_deltas else 0.0
        total_avg = avg_call + avg_put
        skew = None
        if total_avg > 0:
            skew = (avg_call - avg_put) / total_avg
        gamma_concentration = self._compute_concentration(rows, "gamma")
        vega_concentration = self._compute_concentration(rows, "vega")
        return {
            "imbalance": imbalance,
            "skew": skew,
            "gamma_concentration": gamma_concentration,
            "vega_concentration": vega_concentration,
        }

    def _should_recalc(self, last_computed: Optional[datetime]) -> bool:
        if not last_computed:
            return True
        next_allowed = last_computed + timedelta(hours=self.recalc_interval_hours)
        return datetime.utcnow() >= next_allowed

    def ensure_thresholds(self, underlying: str, options_data: Dict[str, Dict]) -> None:
        if not self.enabled:
            return
        dte_bucket = self._primary_bucket_from_options(options_data)
        if not dte_bucket:
            return
        last_computed = self.db.get_threshold_last_computed(underlying, dte_bucket)
        if not self._should_recalc(last_computed):
            return
        try:
            self._recalculate_thresholds(underlying, dte_bucket)
        except Exception as e:
            logger.error(
                f"Ошибка пересчета динамических порогов для {underlying}/{dte_bucket}: {e}",
                exc_info=True,
            )

    def get_thresholds_for_options(self, underlying: str, options_data: Dict[str, Dict]) -> Dict[str, float]:
        """Return thresholds for options; fallback to STRATEGY_CONFIG if missing."""
        thresholds = {
            "ivr_threshold": STRATEGY_CONFIG.get("ivr_threshold", 50.0),
            "delta_imbalance_threshold": STRATEGY_CONFIG.get("delta_imbalance_threshold", 0.1),
            "skew_threshold": STRATEGY_CONFIG.get("skew_threshold", 0.1),
            "gamma_concentration_threshold": STRATEGY_CONFIG.get("gamma_concentration_threshold", 0.1),
            "vega_concentration_threshold": STRATEGY_CONFIG.get("vega_concentration_threshold", 0.1),
            "volume_spike_threshold": None,
            "volume_spike_multiplier": STRATEGY_CONFIG.get("volume_spike_multiplier", 2.0),
        }
        if not self.enabled:
            return thresholds
        dte_bucket = self._primary_bucket_from_options(options_data)
        if not dte_bucket:
            return thresholds
        stored = self.db.get_strategy_thresholds(underlying, dte_bucket)
        for key, value in stored.items():
            if value is not None:
                thresholds[key] = value
        return thresholds

    def _recalculate_thresholds(self, underlying: str, dte_bucket: str) -> Dict[str, int]:
        bucket_range = self._get_bucket_range(dte_bucket)
        if not bucket_range:
            logger.warning(f"Неизвестный DTE-бин: {dte_bucket}")
            return {"dte_bucket": dte_bucket, "missing_data": 0}
        min_dte, max_dte = bucket_range
        since = datetime.utcnow() - timedelta(days=self.lookback_days)
        rows = self.db.get_option_history_for_thresholds(
            underlying=underlying,
            min_dte=min_dte,
            max_dte=max_dte,
            since=since,
        )
        if not rows:
            logger.warning(f"Нет данных для расчета порогов: {underlying}/{dte_bucket}")
            return {"dte_bucket": dte_bucket, "missing_data": 0}
        slices: Dict[str, List[Dict]] = defaultdict(list)
        volume_values: List[float] = []
        iv_values: List[float] = []
        for row in rows:
            slices[row["date_data_collection"]].append(row)
            volume = row.get("volume_24h")
            if volume is not None:
                volume_values.append(volume)
            iv = row.get("iv")
            if iv is not None:
                iv_values.append(iv)
        imbalance_values: List[float] = []
        skew_values: List[float] = []
        gamma_conc_values: List[float] = []
        vega_conc_values: List[float] = []
        for slice_rows in slices.values():
            metrics = self._compute_slice_metrics(slice_rows)
            if metrics["imbalance"] is not None:
                imbalance_values.append(abs(metrics["imbalance"]))
            if metrics["skew"] is not None:
                skew_values.append(abs(metrics["skew"]))
            if metrics["gamma_concentration"] is not None:
                gamma_conc_values.append(metrics["gamma_concentration"])
            if metrics["vega_concentration"] is not None:
                vega_conc_values.append(metrics["vega_concentration"])
        percentile_map = self.percentiles
        calculations = {
            "ivr_threshold": ([], percentile_map.get("ivr_threshold", 85)),
            "delta_imbalance_threshold": (imbalance_values, percentile_map.get("delta_imbalance", 85)),
            "skew_threshold": (skew_values, percentile_map.get("skew", 85)),
            "gamma_concentration_threshold": (gamma_conc_values, percentile_map.get("gamma_concentration", 85)),
            "vega_concentration_threshold": (vega_conc_values, percentile_map.get("vega_concentration", 85)),
            "volume_spike_threshold": (volume_values, percentile_map.get("volume_spike", 95)),
        }
        ivr_values: List[float] = []
        if iv_values:
            min_iv = min(iv_values)
            max_iv = max(iv_values)
            iv_range = max_iv - min_iv
            if iv_range > 0:
                ivr_values = [max(0.0, min(100.0, ((iv - min_iv) / iv_range) * 100.0)) for iv in iv_values]
        calculations["ivr_threshold"] = (ivr_values, percentile_map.get("ivr_threshold", 85))
        insufficient = {}
        for metric, (values, pct) in calculations.items():
            if pct is None:
                continue
            if len(values) < self.min_sample_size:
                logger.info(
                    f"Недостаточно данных для {metric} ({len(values)} < {self.min_sample_size}); "
                    f"используем fallback"
                )
                insufficient[metric] = len(values)
                continue
            value = _percentile(values, pct)
            if value is None:
                continue
            method = f"pct_{pct}"
            self.db.save_strategy_threshold(
                underlying=underlying,
                dte_bucket=dte_bucket,
                metric=metric,
                value=value,
                sample_size=len(values),
                method=method,
            )
            logger.info(
                f"Динамический порог {metric} для {underlying}/{dte_bucket}: {value:.4f} "
                f"(n={len(values)}, {method})"
            )
        return {
            "dte_bucket": dte_bucket,
            "missing_data": insufficient
        }

    def recalculate_for_underlying(self, underlying: str, dte_bucket: Optional[str] = None) -> Dict[str, Dict]:
        """
        Пересчитать пороги для указанного underlying.
        Если dte_bucket не задан - пересчитывает все бины.
        """
        if not self.enabled:
            logger.info("Динамические пороги отключены (enabled=False)")
            return {"underlying": underlying, "insufficient_bins": []}
        insufficient_bins: List[Dict] = []
        if dte_bucket:
            result = self._recalculate_thresholds(underlying, dte_bucket)
            if result.get("missing_data"):
                insufficient_bins.append(result)
            return {"underlying": underlying, "insufficient_bins": insufficient_bins}
        for bucket in DTE_BINS:
            result = self._recalculate_thresholds(underlying, bucket["label"])
            if result.get("missing_data"):
                insufficient_bins.append(result)
        return {"underlying": underlying, "insufficient_bins": insufficient_bins}
